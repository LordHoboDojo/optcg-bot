//! ONE PIECE CARD GAME rules engine (Comprehensive Rules v1.2.1), ported from optcg/engine.py.
//!
//! The Python engine pauses a generator whenever a player must decide. Paused generators can't
//! be copied, so here the game runs in segments instead: each segment starts from a saved
//! state at a Main Phase decision (or the start of the game), and `choose()` either returns the
//! next recorded answer or stops the segment with a Decision. Answering re-runs the segment
//! from its checkpoint with one more answer. Segments are short (one Main Phase action), so the
//! re-runs are cheap, and the whole game is plain data that `clone()` copies.
//!
//! Everything else follows engine.py function by function, including the order things happen
//! in and the text of log lines, prompts and option labels, so the two engines can be compared
//! line by line.

use std::cell::Cell;
use std::sync::Arc;

use smallvec::SmallVec;

use crate::cards::*;
use crate::db::Db;
use crate::rng::PyRandom;

pub const MAX_CHARACTERS: usize = 5;
pub const DON_DECK_SIZE: u32 = 10;
pub const OPENING_HAND: usize = 5;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Setup,
    Refresh,
    Draw,
    Don,
    Main,
    Attack,
    Block,
    Counter,
    Damage,
    EndOfBattle,
    End,
}

impl Phase {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Setup => "setup",
            Self::Refresh => "refresh",
            Self::Draw => "draw",
            Self::Don => "don",
            Self::Main => "main",
            Self::Attack => "attack",
            Self::Block => "block",
            Self::Counter => "counter",
            Self::Damage => "damage",
            Self::EndOfBattle => "end_of_battle",
            Self::End => "end",
        }
    }
}

/// A zone's cards, stored inline so copying a game doesn't allocate (spills past N).
pub type Pile<const N: usize> = SmallVec<[Uid; N]>;

#[derive(Clone, Debug)]
pub struct PlayerState {
    pub leader: Uid,
    // For deck, life and trash the top card is the LAST element.
    pub deck: Pile<56>,
    pub hand: Pile<16>,
    pub trash: Pile<56>,
    pub life: Pile<8>,
    pub characters: Pile<8>,
    pub stage: Option<Uid>,
    pub don_deck: u32,
    pub don_active: u32,
    pub don_rested: u32,
    pub no_character_play_turn: i32,
}

impl PlayerState {
    fn new() -> Self {
        PlayerState {
            leader: 0,
            deck: Pile::new(),
            hand: Pile::new(),
            trash: Pile::new(),
            life: Pile::new(),
            characters: Pile::new(),
            stage: None,
            don_deck: DON_DECK_SIZE,
            don_active: 0,
            don_rested: 0,
            no_character_play_turn: -1,
        }
    }
    /// Leader, Characters, then Stage, without allocating.
    pub fn field_iter(&self) -> impl Iterator<Item = Uid> + '_ {
        std::iter::once(self.leader).chain(self.characters.iter().copied()).chain(self.stage)
    }
    /// Leader, Characters, then Stage.
    pub fn field(&self) -> Vec<Uid> {
        let mut f = Vec::with_capacity(7);
        f.push(self.leader);
        f.extend_from_slice(&self.characters);
        if let Some(s) = self.stage {
            f.push(s);
        }
        f
    }
}

/// An auto effect waiting to activate.
#[derive(Clone, Debug, PartialEq)]
pub struct Pending {
    pub src: Uid,
    pub idx: usize,
    pub inc: u32,
    pub batch: u32,
    pub activated: bool,
}

#[derive(Clone, Debug)]
pub struct Battle {
    pub attacker: Uid,
    pub target: Uid,
    pub attacker_inc: u32,
    pub target_inc: u32,
}

/// Everything about a game that changes as it is played.
#[derive(Clone)]
pub struct State {
    pub rng: PyRandom,
    pub players: [PlayerState; 2],
    pub cards: Vec<Card>,
    pub turn_number: i32,
    pub turn_player: u8,
    pub first_player: u8,
    pub phase: Phase,
    pub battle: Option<Battle>,
    pub queue: Vec<Pending>,
    pub resolving_batch: Option<u32>,
}

/// Why a segment stopped early.
pub enum Stop {
    Ask(Decision),
    Over(Option<u8>),
    /// A replayed answer is no longer an option (only after `determinize` changed hidden cards).
    Diverged,
}

pub type R<T> = Result<T, Stop>;

/// The engine while it runs a segment: the state, the card database, and the answers so far.
pub struct Eng<'a> {
    pub s: &'a mut State,
    pub db: &'a Db,
    answers: &'a [Action],
    pos: usize,
    log: &'a mut Vec<String>,
    logging: bool,
    labels: bool,
    in_static_cost: Cell<bool>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Seg {
    Start,
    Main,
}

impl<'a> Eng<'a> {
    // ------------------------------------------------------------ small accessors

    pub fn c(&self, u: Uid) -> &Card {
        &self.s.cards[u as usize]
    }
    pub fn cm(&mut self, u: Uid) -> &mut Card {
        &mut self.s.cards[u as usize]
    }
    pub fn d(&self, u: Uid) -> &'a CardDef {
        &self.db.defs[self.s.cards[u as usize].def as usize]
    }
    pub fn name(&self, u: Uid) -> &'a str {
        &self.d(u).name
    }
    pub fn owner(&self, u: Uid) -> u8 {
        self.s.cards[u as usize].owner
    }
    pub fn ps(&self, p: u8) -> &PlayerState {
        &self.s.players[p as usize]
    }
    pub fn psm(&mut self, p: u8) -> &mut PlayerState {
        &mut self.s.players[p as usize]
    }

    /// An option label or prompt. Answers compare by kind and cards, never by label, so the
    /// text is skipped while a segment replays recorded answers, or when labels are off.
    pub fn lbl(&self, f: impl FnOnce() -> String) -> String {
        if self.labels && self.pos >= self.answers.len() {
            f()
        } else {
            String::new()
        }
    }

    pub fn log(&mut self, msg: impl FnOnce() -> String) {
        if self.logging {
            let line = format!("[T{}] {}", self.s.turn_number, msg());
            self.log.push(line);
        }
    }

    // ------------------------------------------------------------ choices

    /// Ask `player` to pick one option. A single option is picked automatically.
    pub fn choose(&mut self, player: u8, kind: DecisionKind, options: Vec<Action>, prompt: &str) -> R<Action> {
        self.check_rules()?;
        if options.len() == 1 {
            return Ok(options.into_iter().next().unwrap());
        }
        if self.pos < self.answers.len() {
            let a = &self.answers[self.pos];
            self.pos += 1;
            return options.into_iter().find(|o| o == a).ok_or(Stop::Diverged);
        }
        Err(Stop::Ask(Decision { player, kind, options, prompt: prompt.to_string() }))
    }

    pub fn confirm(&mut self, player: u8, prompt: &str) -> R<bool> {
        let a = self.choose(
            player,
            DecisionKind::Confirm,
            vec![Action::new(ActKind::Yes), Action::new(ActKind::No)],
            prompt,
        )?;
        Ok(a.kind == ActKind::Yes)
    }

    /// Pick up to `up_to` cards one at a time (with a Done option), or exactly that many.
    pub fn choose_cards(&mut self, player: u8, candidates: Vec<Uid>, up_to: usize, prompt: &str, exactly: bool) -> R<Vec<Uid>> {
        let mut cands = candidates;
        let mut chosen = vec![];
        while chosen.len() < up_to && !cands.is_empty() {
            let mut opts = if exactly { vec![] } else { vec![Action::new(ActKind::Pass).label("Done")] };
            for &c in &cands {
                opts.push(Action::new(ActKind::Select).card(c).label(self.lbl(|| self.describe(c))));
            }
            let a = self.choose(player, DecisionKind::Select, opts, prompt)?;
            if a.kind == ActKind::Pass {
                break;
            }
            let c = a.card.unwrap();
            chosen.push(c);
            remove_first(&mut cands, c);
        }
        Ok(chosen)
    }

    // ------------------------------------------------------------ queries

    pub fn characters(&self, p: u8) -> Vec<Uid> {
        self.ps(p).characters.to_vec()
    }
    pub fn leader_and_characters(&self, p: u8) -> Vec<Uid> {
        let ps = self.ps(p);
        let mut v = vec![ps.leader];
        v.extend_from_slice(&ps.characters);
        v
    }
    pub fn negated(&self, u: Uid) -> bool {
        self.c(u).mods.iter().any(|m| m.kind == ModKind::Negated)
    }
    pub fn negate(&mut self, u: Uid, until: Until, by: Option<u8>) {
        let player = by.unwrap_or(self.s.turn_player);
        self.cm(u).mods.push(Mod { kind: ModKind::Negated, until, player });
    }

    /// Sources of permanent effects: every card on the field, player 0 first, unless negated.
    pub fn static_sources(&self) -> impl Iterator<Item = Uid> + '_ {
        self.s.players.iter().flat_map(|ps| ps.field_iter()).filter(move |&u| !self.negated(u))
    }

    pub fn has_keyword(&self, u: Uid, kw: Kw) -> bool {
        if self.d(u).keywords.contains(&kw) && !self.negated(u) {
            return true;
        }
        if self.c(u).mods.iter().any(|m| m.kind == ModKind::Keyword(kw)) {
            return true;
        }
        self.static_sources().any(|src| self.static_keyword(src, u, kw))
    }

    pub fn base_power(&self, u: Uid) -> i32 {
        let mut best: Option<i32> = None;
        for m in &self.c(u).mods {
            if let ModKind::BasePower(v) = m.kind {
                best = Some(best.map_or(v, |b| b.max(v)));
            }
        }
        for src in self.static_sources() {
            if let Some(f) = self.d(src).st.base_power {
                if let Some(v) = f(self, src, u) {
                    best = Some(best.map_or(v, |b| b.max(v)));
                }
            }
        }
        best.unwrap_or(self.d(u).power) // 4-9-2-1: highest value wins
    }

    pub fn power(&self, u: Uid) -> i32 {
        let c = self.c(u);
        let mut p = self.base_power(u);
        for m in &c.mods {
            if let ModKind::Power(v) = m.kind {
                p += v;
            }
        }
        if c.don > 0 && self.s.turn_player == c.owner {
            p += 1000 * c.don as i32; // 6-5-5-2
        }
        for src in self.static_sources() {
            p += self.static_power(src, u);
        }
        p // may be negative (1-3-6-1)
    }

    pub fn cost(&self, u: Uid) -> i32 {
        let c = self.c(u);
        let mut v = self.d(u).cost;
        for m in &c.mods {
            if let ModKind::Cost(x) = m.kind {
                v += x;
            }
        }
        if !self.in_static_cost.get() {
            // a cost effect whose condition reads costs sees printed ones
            self.in_static_cost.set(true);
            for src in self.static_sources() {
                v += self.static_cost(src, u);
            }
            if c.zone == Some(Zone::Hand) {
                v += self.hand_cost(u);
            }
            self.in_static_cost.set(false);
        }
        v.max(0)
    }

    /// 2-10-4: with several Counters, the highest applies.
    pub fn counter_value(&self, u: Uid) -> i32 {
        let mut best = self.d(u).counter;
        if self.c(u).zone == Some(Zone::Hand) {
            if let Some(f) = self.d(u).st.hand_counter {
                best = best.max(f(self, u).unwrap_or(0));
            }
        }
        for src in self.static_sources() {
            if let Some(f) = self.d(src).st.counter {
                best = best.max(f(self, src, u).unwrap_or(0));
            }
        }
        best
    }

    pub fn describe(&self, u: Uid) -> String {
        let d = self.d(u);
        let mut bits: Vec<String> = vec![];
        if matches!(d.category, Category::Character | Category::Event | Category::Stage) {
            bits.push(format!("cost {}", self.cost(u)));
        }
        if matches!(d.category, Category::Leader | Category::Character) {
            bits.push(format!("{} power", self.power(u)));
        }
        if self.c(u).rested {
            bits.push("rested".into());
        }
        if bits.is_empty() {
            d.name.clone()
        } else {
            format!("{} ({})", d.name, bits.join(", "))
        }
    }

    // ------------------------------------------------------------ card movement

    fn create_card(&mut self, def: u16, owner: u8, zone: Zone) -> Uid {
        let uid = self.s.cards.len() as Uid;
        self.s.cards.push(Card {
            uid,
            def,
            owner,
            zone: None,
            face_up: false,
            incarnation: 0,
            rested: false,
            don: 0,
            played_turn: -1,
            mods: SmallVec::new(),
            opt_used: SmallVec::new(),
        });
        if zone == Zone::Leader {
            self.psm(owner).leader = uid;
            self.cm(uid).zone = Some(Zone::Leader);
        } else {
            self.move_card(uid, Some(zone), false, false);
        }
        uid
    }

    /// Apply `f` to the owner's list for a zone that is one (not Leader, Stage or limbo).
    fn with_zone<T>(&mut self, owner: u8, zone: Option<Zone>, f: impl FnOnce(&mut dyn PileOps) -> T) -> Option<T> {
        let ps = &mut self.s.players[owner as usize];
        Some(match zone? {
            Zone::Deck => f(&mut ps.deck),
            Zone::Hand => f(&mut ps.hand),
            Zone::Trash => f(&mut ps.trash),
            Zone::Life => f(&mut ps.life),
            Zone::Character => f(&mut ps.characters),
            _ => return None,
        })
    }

    /// Move a card to its owner's zone. zone=None puts it in limbo (a [Trigger] resolving).
    pub fn move_card(&mut self, u: Uid, zone: Option<Zone>, bottom: bool, face_up: bool) {
        let (owner, old, don) = {
            let c = self.c(u);
            (c.owner, c.zone, c.don)
        };
        if old == Some(Zone::Leader) {
            return; // Leaders never leave the Leader area (3-6-3)
        }
        let in_list = self.with_zone(owner, old, |l| l.remove_uid(u)).is_some();
        if !in_list && old == Some(Zone::Stage) {
            self.psm(owner).stage = None;
        }
        if don > 0 {
            // 6-5-5-4: given DON!! go back to the cost area rested
            self.psm(owner).don_rested += don;
            self.cm(u).don = 0;
        }
        let c = self.cm(u);
        c.reset();
        c.zone = zone;
        c.face_up = face_up && zone == Some(Zone::Life);
        if zone == Some(Zone::Stage) {
            self.psm(owner).stage = Some(u);
        } else if zone.is_some() {
            self.with_zone(owner, zone, |l| l.put(u, bottom)).expect("unknown zone");
        }
    }

    pub fn mv(&mut self, u: Uid, zone: Zone) {
        self.move_card(u, Some(zone), false, false);
    }

    // ------------------------------------------------------------ effect helpers

    pub fn draw(&mut self, p: u8, n: usize) {
        for _ in 0..n {
            match self.ps(p).deck.last() {
                Some(&top) => self.mv(top, Zone::Hand),
                None => return,
            }
        }
    }

    fn add_mod(&mut self, u: Uid, kind: ModKind, until: Until, by: Option<u8>) {
        let player = by.unwrap_or(self.s.turn_player);
        self.cm(u).mods.push(Mod { kind, until, player });
    }
    pub fn add_power(&mut self, u: Uid, amount: i32, until: Until, by: Option<u8>) {
        self.add_mod(u, ModKind::Power(amount), until, by);
    }
    pub fn add_cost(&mut self, u: Uid, amount: i32, until: Until, by: Option<u8>) {
        self.add_mod(u, ModKind::Cost(amount), until, by);
    }
    pub fn grant_keyword(&mut self, u: Uid, kw: Kw, until: Until, by: Option<u8>) {
        self.add_mod(u, ModKind::Keyword(kw), until, by);
    }
    pub fn set_base_power(&mut self, u: Uid, v: i32, until: Until, by: Option<u8>) {
        self.add_mod(u, ModKind::BasePower(v), until, by);
    }
    /// The card won't become active in its owner's next Refresh Phase.
    pub fn skip_next_refresh(&mut self, u: Uid, by: u8) {
        self.add_mod(u, ModKind::NoRefresh, Until::Permanent, Some(by));
    }

    pub fn trash_from_hand(&mut self, p: u8, n: usize) -> R<Vec<Uid>> {
        let hand = self.ps(p).hand.to_vec();
        let cards = self.choose_cards(p, hand, n, &format!("Trash {n} card(s) from your hand"), true)?;
        for &c in &cards {
            self.mv(c, Zone::Trash);
        }
        Ok(cards)
    }

    /// Move up to n DON!! from the DON!! deck to the cost area. Returns how many moved.
    pub fn add_don(&mut self, p: u8, n: u32, rested: bool) -> u32 {
        let ps = self.psm(p);
        let n = n.min(ps.don_deck);
        ps.don_deck -= n;
        if rested {
            ps.don_rested += n;
        } else {
            ps.don_active += n;
        }
        n
    }

    pub fn give_don(&mut self, p: u8, target: Uid, n: u32, from_rested: bool) -> u32 {
        let ps = self.psm(p);
        let n = if from_rested {
            let n = n.min(ps.don_rested);
            ps.don_rested -= n;
            n
        } else {
            let n = n.min(ps.don_active);
            ps.don_active -= n;
            n
        };
        self.cm(target).don += n;
        n
    }

    /// Offer the owner's replacement effects for `card` leaving the field. True if one was used.
    fn replaced(&mut self, card: Uid, by: Option<u8>, ko: bool) -> R<bool> {
        let owner = self.owner(card);
        for src in self.ps(owner).field() {
            if self.negated(src) {
                continue;
            }
            for r in &self.d(src).st.replacements {
                if !((r.applies)(self, src, card, by, ko) && (r.payable)(self, src)) {
                    continue;
                }
                if self.confirm(owner, &format!("{}: {} instead?", self.name(src), r.text))? {
                    let (s, c) = (self.name(src), self.name(card));
                    self.log(|| format!("{s} replaces the removal of {c}"));
                    (r.pay)(self, src, card)?;
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }

    /// Move a Character or Stage off the field by an effect of player `by` (not a K.O.).
    pub fn remove_from_field(&mut self, card: Uid, zone: Zone, by: Option<u8>, bottom: bool, face_up: bool) -> R<bool> {
        if !matches!(self.c(card).zone, Some(Zone::Character) | Some(Zone::Stage)) {
            return Ok(false);
        }
        if self.replaced(card, by, false)? {
            return Ok(false);
        }
        self.move_card(card, Some(zone), bottom, face_up);
        Ok(true)
    }

    /// 3-1-7: when several cards go to an area at once, their owner picks the order.
    pub fn place_in_order(&mut self, p: u8, cards: Vec<Uid>, zone: Zone, bottom: bool) -> R<()> {
        let zname = match zone {
            Zone::Deck => "deck",
            Zone::Life => "life",
            Zone::Trash => "trash",
            Zone::Hand => "hand",
            _ => "field",
        };
        let mut left = cards;
        let mut order = vec![];
        while left.len() > 1 {
            let opts = left.iter().map(|&c| Action::new(ActKind::Select).card(c).label(self.lbl(|| self.name(c).to_string()))).collect();
            let a = self.choose(
                p,
                DecisionKind::OrderCards,
                opts,
                &format!("Order the cards going to your {zname}: pick the next one from the top"),
            )?;
            let c = a.card.unwrap();
            order.push(c);
            remove_first(&mut left, c);
        }
        order.extend(left);
        if bottom {
            for c in order {
                self.move_card(c, Some(zone), true, false);
            }
        } else {
            for c in order.into_iter().rev() {
                self.mv(c, zone);
            }
        }
        Ok(())
    }

    /// Cost "rest N of your cards": any active Leader, Character, Stage or DON!!.
    pub fn rest_your_cards(&mut self, p: u8, n: usize) -> R<()> {
        for _ in 0..n {
            let mut opts: Vec<Action> = self
                .ps(p)
                .field()
                .into_iter()
                .filter(|&c| !self.c(c).rested)
                .map(|c| Action::new(ActKind::Select).card(c).label(self.lbl(|| format!("Rest {}", self.name(c)))))
                .collect();
            if self.ps(p).don_active > 0 {
                opts.push(Action::new(ActKind::Select).index(0).label("Rest 1 active DON!!"));
            }
            if opts.is_empty() {
                return Ok(());
            }
            let a = self.choose(p, DecisionKind::Select, opts, "Rest 1 of your cards")?;
            match a.card {
                None => {
                    let ps = self.psm(p);
                    ps.don_active -= 1;
                    ps.don_rested += 1;
                }
                Some(c) => self.cm(c).rested = true,
            }
        }
        Ok(())
    }

    pub fn active_card_count(&self, p: u8) -> u32 {
        self.ps(p).field().into_iter().filter(|&c| !self.c(c).rested).count() as u32 + self.ps(p).don_active
    }

    pub fn forbid_character_play(&mut self, p: u8) {
        let t = self.s.turn_number;
        self.psm(p).no_character_play_turn = t;
    }

    fn ko_prevented(&self, card: Uid, by: Option<u8>, in_battle: bool) -> bool {
        if in_battle {
            return self.has_keyword(card, Kw::NoKoInBattle);
        }
        if self.has_keyword(card, Kw::NoKoByEffects) {
            return true;
        }
        by == Some(1 - self.owner(card)) && self.has_keyword(card, Kw::NoKoByOppEffects)
    }

    /// K.O. a Character (10-2-1). [On K.O.] effects activate on the field and resolve from
    /// the trash (10-2-17).
    pub fn ko(&mut self, card: Uid, by: Option<u8>, replaceable: bool, in_battle: bool) -> R<()> {
        if self.c(card).zone != Some(Zone::Character) {
            return Ok(());
        }
        let name = self.name(card);
        if self.ko_prevented(card, by, in_battle) {
            self.log(|| format!("{name} cannot be K.O.'d"));
            return Ok(());
        }
        if replaceable && self.replaced(card, by, true)? {
            return Ok(());
        }
        let ready: Vec<usize> =
            self.d(card).effect_indexes(Timing::OnKo).filter(|&i| self.can_activate(card, i)).collect();
        self.log(|| format!("{name} is K.O.'d"));
        let mut paid = vec![];
        let owner = self.owner(card);
        for i in ready {
            let eff = &self.d(card).effects[i];
            if eff.optional && !self.confirm(owner, &format!("Activate {}: {}?", name, eff.text))? {
                continue;
            }
            if self.pay_costs(card, i)? {
                paid.push(i);
            }
        }
        self.mv(card, Zone::Trash);
        for i in paid {
            let p = Pending { src: card, idx: i, inc: self.c(card).incarnation, batch: self.next_batch(), activated: true };
            self.s.queue.push(p);
        }
        Ok(())
    }

    /// Play a Character or Stage without paying its cost.
    pub fn play_card(&mut self, card: Uid, rested: bool) -> R<()> {
        match self.d(card).category {
            Category::Character => self.play_character(card, rested),
            Category::Stage => {
                self.play_stage(card);
                Ok(())
            }
            c => panic!("cannot play {c:?}"),
        }
    }

    /// Damage processing (4-6, 7-1-4-1-1).
    pub fn deal_damage(&mut self, p: u8, amount: u32, banish: bool) -> R<()> {
        for _ in 0..amount {
            let Some(&card) = self.ps(p).life.last() else {
                self.log(|| format!("Player {p} takes damage with 0 Life"));
                return Err(Stop::Over(Some(1 - p)));
            };
            if banish {
                self.mv(card, Zone::Trash);
                self.log(|| format!("Player {p}'s Life card is trashed (Banish)"));
                continue;
            }
            let trig: Vec<usize> =
                self.d(card).effect_indexes(Timing::Trigger).filter(|&i| self.can_activate(card, i)).collect();
            let name = self.name(card);
            let activate = !trig.is_empty() && {
                let opts = vec![
                    Action::new(ActKind::Yes).card(card).label(self.lbl(|| format!("Activate [Trigger] of {name}"))),
                    Action::new(ActKind::No).label("Add to hand"),
                ];
                self.choose(p, DecisionKind::Trigger, opts, &format!("{name} has [Trigger]"))?.kind == ActKind::Yes
            };
            if activate {
                self.move_card(card, None, false, false); // in no area while it resolves (10-1-5-3)
                self.log(|| format!("Player {p} activates [Trigger] of {name}"));
                self.resolve(card, trig[0])?;
                if self.c(card).zone.is_none() {
                    self.mv(card, Zone::Trash);
                }
            } else {
                self.mv(card, Zone::Hand);
                let left = self.ps(p).life.len();
                self.log(|| format!("Player {p} adds a Life card to hand ({left} left)"));
            }
        }
        Ok(())
    }

    // ------------------------------------------------------------ effects

    pub fn can_activate(&self, src: Uid, idx: usize) -> bool {
        let d = self.d(src);
        let eff = &d.effects[idx];
        let c = self.c(src);
        let p = c.owner;
        let ps = self.ps(p);
        if self.negated(src) {
            return false;
        }
        if eff.your_turn && self.s.turn_player != p {
            return false;
        }
        if eff.opp_turn && self.s.turn_player == p {
            return false;
        }
        if eff.don_req > 0 && c.don < eff.don_req {
            return false;
        }
        if eff.once_per_turn && c.opt_turn(eff.opt_group.unwrap_or(idx)) == Some(self.s.turn_number) {
            return false;
        }
        if eff.rest_self && c.rested {
            return false;
        }
        if eff.rest_don > ps.don_active {
            return false;
        }
        if eff.don_minus > self.don_on_field(p) {
            return false;
        }
        if let Some(cond) = &eff.condition {
            if !self.eval(cond, src) {
                return false;
            }
        }
        true
    }

    pub fn don_on_field(&self, p: u8) -> u32 {
        let ps = self.ps(p);
        ps.don_active + ps.don_rested + ps.field().into_iter().map(|c| self.c(c).don).sum::<u32>()
    }

    /// Pay an effect's activation cost (8-3). True if paid.
    fn pay_costs(&mut self, src: Uid, idx: usize) -> R<bool> {
        if !self.can_activate(src, idx) {
            return Ok(false);
        }
        let eff = &self.d(src).effects[idx];
        let owner = self.owner(src);
        if eff.once_per_turn {
            let t = self.s.turn_number;
            self.cm(src).set_opt(eff.opt_group.unwrap_or(idx), t);
        }
        if eff.rest_self {
            self.cm(src).rested = true;
        }
        if eff.rest_don > 0 {
            let ps = self.psm(owner);
            ps.don_active -= eff.rest_don;
            ps.don_rested += eff.rest_don;
        }
        if eff.don_minus > 0 {
            self.return_don(owner, eff.don_minus)?;
        }
        Ok(true)
    }

    /// DON!! -X (8-3-1-6, 10-2-10).
    pub fn return_don(&mut self, p: u8, n: u32) -> R<()> {
        for _ in 0..n {
            let mut opts = vec![];
            if self.ps(p).don_rested > 0 {
                opts.push(Action::new(ActKind::Select).index(0).label("Rested DON!! in your cost area"));
            }
            if self.ps(p).don_active > 0 {
                opts.push(Action::new(ActKind::Select).index(1).label("Active DON!! in your cost area"));
            }
            for c in self.ps(p).field() {
                if self.c(c).don > 0 {
                    opts.push(Action::new(ActKind::Select).card(c).label(self.lbl(|| format!("DON!! given to {}", self.name(c)))));
                }
            }
            if opts.is_empty() {
                break;
            }
            let a = self.choose(p, DecisionKind::ReturnDon, opts, "Return 1 DON!! to your DON!! deck")?;
            if let Some(c) = a.card {
                self.cm(c).don -= 1;
            } else if a.index == Some(0) {
                self.psm(p).don_rested -= 1;
            } else {
                self.psm(p).don_active -= 1;
            }
            self.psm(p).don_deck += 1;
        }
        let field = self.ps(p).field();
        self.fire(Timing::DonReturned, &field);
        Ok(())
    }

    pub fn resolve(&mut self, src: Uid, idx: usize) -> R<()> {
        let eff = &self.d(src).effects[idx];
        let name = self.name(src);
        self.log(|| {
            let text = if eff.text.starts_with('[') {
                eff.text.clone()
            } else {
                format!("[{}] {}", eff.timing.as_str(), eff.text)
            };
            format!("{name}: {text}").trim_end().to_string()
        });
        self.run(&eff.body, src)
    }

    pub fn activate(&mut self, src: Uid, idx: usize) -> R<()> {
        if self.pay_costs(src, idx)? {
            self.resolve(src, idx)?;
        }
        Ok(())
    }

    // Triggered (auto) effects queue and resolve at the next safe point (8-6); see engine.py.

    fn next_batch(&self) -> u32 {
        self.s.resolving_batch.map_or(0, |b| b + 1)
    }

    /// Queue every auto effect with this timing on these cards whose conditions are met now.
    pub fn fire(&mut self, timing: Timing, sources: &[Uid]) {
        for &src in sources {
            let idxs: Vec<usize> = self.d(src).effect_indexes(timing).collect();
            for i in idxs {
                if self.can_activate(src, i) {
                    let p = Pending { src, idx: i, inc: self.c(src).incarnation, batch: self.next_batch(), activated: false };
                    self.s.queue.push(p);
                }
            }
        }
    }

    /// Resolve queued auto effects until none are left.
    pub fn resolve_triggers(&mut self) -> R<()> {
        while !self.s.queue.is_empty() {
            let batch = self.s.queue.iter().map(|q| q.batch).min().unwrap();
            let tp = self.s.turn_player;
            let mut mine: Vec<Pending> =
                self.s.queue.iter().filter(|q| q.batch == batch && self.owner(q.src) == tp).cloned().collect();
            if mine.is_empty() {
                mine = self.s.queue.iter().filter(|q| q.batch == batch).cloned().collect();
            }
            let mut q = mine[0].clone();
            if mine.len() > 1 {
                let opts = mine
                    .iter()
                    .map(|x| {
                        Action::new(ActKind::Select).card(x.src).index(x.idx as i32).label(self.lbl(|| format!(
                            "{}: {}",
                            self.name(x.src),
                            self.d(x.src).effects[x.idx].text
                        )))
                    })
                    .collect();
                let a = self.choose(self.owner(q.src), DecisionKind::Order, opts, "Choose which effect resolves next")?;
                q = mine
                    .iter()
                    .find(|x| Some(x.src) == a.card && Some(x.idx as i32) == a.index)
                    .unwrap()
                    .clone();
            }
            let at = self.s.queue.iter().position(|x| *x == q).unwrap();
            self.s.queue.remove(at);
            let outer = self.s.resolving_batch.replace(batch);
            let r = self.activate_pending(&q);
            if !matches!(r, Err(Stop::Ask(_))) {
                self.s.resolving_batch = outer; // the `finally` in engine.py
            }
            r?;
        }
        Ok(())
    }

    fn activate_pending(&mut self, q: &Pending) -> R<()> {
        let src = q.src;
        if q.activated {
            return self.resolve(src, q.idx);
        }
        if self.c(src).incarnation != q.inc {
            return Ok(()); // the card moved before its effect activated (8-1-3-1-3)
        }
        if !self.can_activate(src, q.idx) {
            return Ok(());
        }
        let eff = &self.d(src).effects[q.idx];
        if eff.optional && !self.confirm(self.owner(src), &format!("Activate {}: {}?", self.name(src), eff.text))? {
            return Ok(());
        }
        self.activate(src, q.idx)
    }

    fn ordered_field(&self) -> Vec<Uid> {
        let tp = self.s.turn_player;
        let mut v = self.ps(tp).field();
        v.extend(self.ps(1 - tp).field());
        v
    }

    fn expire(&mut self, until: Until, player: Option<u8>) {
        for c in self.s.cards.iter_mut() {
            if !c.mods.is_empty() {
                c.mods.retain(|m| !(m.until == until && player.map_or(true, |p| m.player == p)));
            }
        }
    }

    // ------------------------------------------------------------ rule processing

    /// Defeat judgment (9-2): a player with 0 cards in deck loses.
    pub fn check_rules(&mut self) -> R<()> {
        let losers: Vec<u8> = (0..2u8).filter(|&p| self.ps(p).deck.is_empty()).collect();
        match losers.len() {
            2 => Err(Stop::Over(None)),
            1 => {
                let l = losers[0];
                self.log(|| format!("Player {l} has no cards in deck"));
                Err(Stop::Over(Some(1 - l)))
            }
            _ => Ok(()),
        }
    }

    // ------------------------------------------------------------ game flow

    fn segment(&mut self, seg: Seg) -> R<Seg> {
        match seg {
            Seg::Start => {
                self.setup()?;
                self.turn_begin()?;
            }
            Seg::Main => {
                let p = self.s.turn_player;
                let opts = self.main_options(p);
                let a = self.choose(p, DecisionKind::Main, opts, "Main Phase")?;
                if a.kind == ActKind::End {
                    self.turn_end()?;
                    self.s.turn_player = 1 - self.s.turn_player;
                    self.turn_begin()?;
                } else {
                    self.do_main_action(p, &a)?;
                    self.resolve_triggers()?;
                    self.check_rules()?;
                }
            }
        }
        Ok(Seg::Main)
    }

    fn setup(&mut self) -> R<()> {
        let chooser = self.s.rng.randrange(2) as u8;
        let a = self.choose(
            chooser,
            DecisionKind::GoFirst,
            vec![Action::new(ActKind::First), Action::new(ActKind::Second)],
            "Go first or second?",
        )?;
        let first = if a.kind == ActKind::First { chooser } else { 1 - chooser };
        self.s.first_player = first;
        self.s.turn_player = first;
        let order = [first, 1 - first];
        self.log(|| format!("Player {first} goes first"));

        // 5-2-1-5-1: the player who chose resolves "at the start of the game" first.
        for p in [chooser, 1 - chooser] {
            let leader = self.ps(p).leader;
            self.fire(Timing::StartOfGame, &[leader]);
            self.resolve_triggers()?;
            let mut deck = std::mem::take(&mut self.psm(p).deck);
            self.s.rng.shuffle(&mut deck);
            self.psm(p).deck = deck;
        }
        for p in order {
            self.draw(p, OPENING_HAND);
        }
        for p in order {
            let a = self.choose(
                p,
                DecisionKind::Mulligan,
                vec![Action::new(ActKind::Keep), Action::new(ActKind::Mulligan)],
                "Keep this hand or redraw once?",
            )?;
            if a.kind == ActKind::Mulligan {
                for c in self.ps(p).hand.to_vec() {
                    self.mv(c, Zone::Deck);
                }
                let mut deck = std::mem::take(&mut self.psm(p).deck);
                self.s.rng.shuffle(&mut deck);
                self.psm(p).deck = deck;
                self.draw(p, OPENING_HAND);
                self.log(|| format!("Player {p} redraws"));
            }
        }
        for p in order {
            // 5-2-1-7: top card of the deck ends up at the bottom of Life
            let life = self.d(self.ps(p).leader).life;
            for _ in 0..life {
                let top = *self.ps(p).deck.last().unwrap();
                self.mv(top, Zone::Life);
            }
        }
        Ok(())
    }

    /// Refresh, Draw and DON!! Phases, and the start of the Main Phase.
    fn turn_begin(&mut self) -> R<()> {
        let p = self.s.turn_player;
        self.s.turn_number += 1;
        self.log(|| format!("--- Player {p}'s turn ---"));

        // Refresh Phase (6-2)
        self.s.phase = Phase::Refresh;
        self.expire(Until::NextTurn, Some(p));
        let field = self.ordered_field();
        self.fire(Timing::StartOfTurn, &field); // 6-2-2
        self.resolve_triggers()?;
        for c in self.ps(p).field() {
            let don = self.c(c).don;
            self.psm(p).don_rested += don;
            self.cm(c).don = 0;
        }
        for c in self.ps(p).field() {
            let card = self.cm(c);
            if card.mods.iter().any(|m| m.kind == ModKind::NoRefresh) {
                card.mods.retain(|m| m.kind != ModKind::NoRefresh);
            } else {
                card.rested = false;
            }
        }
        let ps = self.psm(p);
        ps.don_active += ps.don_rested;
        ps.don_rested = 0;

        // Draw Phase (6-3)
        self.s.phase = Phase::Draw;
        if self.s.turn_number != 1 {
            self.draw(p, 1);
        }

        // DON!! Phase (6-4)
        self.s.phase = Phase::Don;
        let n = if self.s.turn_number == 1 { 1 } else { 2 };
        self.add_don(p, n, false);

        // Main Phase (6-5)
        self.s.phase = Phase::Main;
        let field = self.ordered_field();
        self.fire(Timing::StartOfMain, &field); // 6-5-1
        self.resolve_triggers()
    }

    /// End Phase (6-6).
    fn turn_end(&mut self) -> R<()> {
        let p = self.s.turn_player;
        self.s.phase = Phase::End;
        let mine = self.ps(p).field();
        self.fire(Timing::EndOfYourTurn, &mine); // 6-6-1-1-2: all of these first
        self.resolve_triggers()?;
        let theirs = self.ps(1 - p).field();
        self.fire(Timing::EndOfOppTurn, &theirs);
        self.resolve_triggers()?;
        self.expire(Until::Turn, None);
        self.expire(Until::NextEnd, Some(1 - p)); // "until the end of your opponent's next End Phase"
        self.check_rules()
    }

    pub fn main_options(&self, p: u8) -> Vec<Action> {
        let ps = self.ps(p);
        let mut opts = vec![];
        for &c in &ps.hand {
            let cost = self.cost(c);
            if cost > ps.don_active as i32 {
                continue;
            }
            let d = self.d(c);
            if d.category == Category::Character && ps.no_character_play_turn == self.s.turn_number {
                continue;
            }
            match d.category {
                Category::Character | Category::Stage => {
                    opts.push(Action::new(ActKind::Play).card(c).label(self.lbl(|| format!("Play {}", self.describe(c)))));
                }
                Category::Event => {
                    if let Some(i) = d.first_effect(Timing::Main) {
                        if self.can_activate(c, i) {
                            opts.push(Action::new(ActKind::Play).card(c).label(self.lbl(|| format!("Activate {}", self.describe(c)))));
                        }
                    }
                }
                Category::Leader => {}
            }
        }
        for c in ps.field() {
            for i in self.d(c).effect_indexes(Timing::ActivateMain) {
                if self.can_activate(c, i) {
                    opts.push(Action::new(ActKind::Activate).card(c).index(i as i32).label(self.lbl(|| format!(
                        "Activate {}: {}",
                        self.name(c),
                        self.d(c).effects[i].text
                    ))));
                }
            }
        }
        if ps.don_active > 0 {
            for c in self.leader_and_characters(p) {
                opts.push(Action::new(ActKind::GiveDon).target(c).label(self.lbl(|| format!("Give 1 DON!! to {}", self.name(c)))));
            }
        }
        if self.s.turn_number > 2 {
            // 6-5-6-1: nobody battles on their first turn
            for atk in self.leader_and_characters(p) {
                if self.c(atk).rested || self.has_keyword(atk, Kw::CannotAttack) {
                    continue;
                }
                for tgt in self.attack_targets(atk) {
                    opts.push(Action::new(ActKind::Attack).card(atk).target(tgt).label(self.lbl(|| format!(
                        "{} ({}) attacks {}",
                        self.name(atk),
                        self.power(atk),
                        self.describe(tgt)
                    ))));
                }
            }
        }
        opts.push(Action::new(ActKind::End).label("End turn"));
        opts
    }

    fn attack_targets(&self, atk: Uid) -> Vec<Uid> {
        let o = 1 - self.owner(atk);
        let opp = self.ps(o);
        let mut targets = vec![opp.leader];
        targets.extend(opp.characters.iter().copied().filter(|&c| self.c(c).rested));
        let locks: Vec<&str> = self
            .static_sources()
            .filter(|&src| self.owner(src) != self.owner(atk))
            .filter(|&src| self.d(src).st.attack_lock.map_or(false, |f| f(self, src)))
            .map(|src| self.name(src))
            .collect();
        if !locks.is_empty() {
            // "your opponent cannot attack any card other than the Character [X]"
            targets.retain(|&c| self.d(c).category == Category::Character && locks.contains(&self.name(c)));
        }
        if self.d(atk).category == Category::Character && self.c(atk).played_turn == self.s.turn_number {
            if self.has_keyword(atk, Kw::Rush) {
                return targets;
            }
            if self.has_keyword(atk, Kw::RushCharacter) {
                return targets.into_iter().filter(|&c| self.d(c).category == Category::Character).collect();
            }
            return vec![]; // 3-7-4
        }
        targets
    }

    fn do_main_action(&mut self, p: u8, a: &Action) -> R<()> {
        match a.kind {
            ActKind::Play => {
                let card = a.card.unwrap();
                self.pay_card_cost(card);
                if self.d(card).category == Category::Event {
                    self.activate_event(card, Timing::Main)
                } else {
                    self.play_card(card, false)
                }
            }
            ActKind::Activate => self.activate(a.card.unwrap(), a.index.unwrap() as usize),
            ActKind::GiveDon => {
                self.give_don(p, a.target.unwrap(), 1, false);
                Ok(())
            }
            ActKind::Attack => self.battle(a.card.unwrap(), a.target.unwrap()),
            _ => Ok(()),
        }
    }

    fn pay_card_cost(&mut self, card: Uid) {
        let cost = self.cost(card) as u32;
        let ps = self.psm(self.owner(card));
        ps.don_active -= cost;
        ps.don_rested += cost;
    }

    /// 8-4-2: trash the Event, then carry out its effect.
    fn activate_event(&mut self, card: Uid, timing: Timing) -> R<()> {
        let idx = self.d(card).first_effect(timing).unwrap();
        let (owner, name) = (self.owner(card), self.name(card));
        self.log(|| format!("Player {owner} activates {name}"));
        self.mv(card, Zone::Trash);
        self.activate(card, idx)
    }

    fn play_character(&mut self, card: Uid, rested: bool) -> R<()> {
        let owner = self.owner(card);
        if self.ps(owner).no_character_play_turn == self.s.turn_number {
            return Ok(());
        }
        if self.ps(owner).characters.len() >= MAX_CHARACTERS {
            // 3-7-6-1: rule processing, not a K.O.
            let chars = self.characters(owner);
            let victim = self.choose_cards(owner, chars, 1, "Character area is full: trash 1", true)?[0];
            self.mv(victim, Zone::Trash);
            let vn = self.name(victim);
            self.log(|| format!("{vn} is trashed to make room"));
        }
        self.mv(card, Zone::Character);
        let t = self.s.turn_number;
        let c = self.cm(card);
        c.rested = rested;
        c.played_turn = t;
        let name = self.name(card);
        self.log(|| format!("Player {owner} plays {name}"));
        self.fire(Timing::OnPlay, &[card]);
        Ok(())
    }

    fn play_stage(&mut self, card: Uid) {
        let owner = self.owner(card);
        if let Some(old) = self.ps(owner).stage {
            self.mv(old, Zone::Trash);
        }
        self.mv(card, Zone::Stage);
        self.cm(card).played_turn = self.s.turn_number;
        let name = self.name(card);
        self.log(|| format!("Player {owner} plays {name}"));
        self.fire(Timing::OnPlay, &[card]);
    }

    // ------------------------------------------------------------ battle (7)

    fn battle_interrupted(&self) -> bool {
        let b = self.s.battle.as_ref().unwrap();
        self.c(b.attacker).incarnation != b.attacker_inc || self.c(b.target).incarnation != b.target_inc
    }

    fn battle(&mut self, attacker: Uid, target: Uid) -> R<()> {
        self.s.battle = Some(Battle {
            attacker,
            target,
            attacker_inc: self.c(attacker).incarnation,
            target_inc: self.c(target).incarnation,
        });
        let r = self.battle_steps().and_then(|_| {
            // End of the Battle (7-1-5), also reached when a step was cut short
            self.s.phase = Phase::EndOfBattle;
            let field = self.ordered_field();
            self.fire(Timing::EndOfBattle, &field);
            self.resolve_triggers()
        });
        if !matches!(r, Err(Stop::Ask(_))) {
            self.end_battle(); // the `finally` in engine.py
        }
        r
    }

    fn end_battle(&mut self) {
        self.expire(Until::Battle, None);
        self.s.battle = None;
        self.s.phase = Phase::Main;
    }

    fn battle_steps(&mut self) -> R<()> {
        let attacker = self.s.battle.as_ref().unwrap().attacker;
        let o = 1 - self.owner(attacker);
        // Attack Step
        self.s.phase = Phase::Attack;
        self.cm(attacker).rested = true;
        let tgt = self.s.battle.as_ref().unwrap().target;
        let (an, ap, td) = (self.name(attacker), self.power(attacker), self.describe(tgt));
        self.log(|| format!("{an} ({ap}) attacks {td}"));
        self.fire(Timing::WhenAttacking, &[attacker]);
        let opp_field = self.ps(o).field();
        self.fire(Timing::OnOppAttack, &opp_field); // 10-2-16-1: after [When Attacking]
        let field = self.ordered_field();
        self.fire(Timing::AttackDeclared, &field);
        self.resolve_triggers()?;
        if self.battle_interrupted() {
            return Ok(()); // 7-1-1-4
        }

        // Block Step
        self.s.phase = Phase::Block;
        if !self.has_keyword(attacker, Kw::Unblockable) {
            let tgt = self.s.battle.as_ref().unwrap().target;
            let blockers: Vec<Uid> = self
                .ps(o)
                .characters
                .iter()
                .copied()
                .filter(|&c| c != tgt && !self.c(c).rested && self.has_keyword(c, Kw::Blocker))
                .collect();
            if !blockers.is_empty() {
                let mut opts = vec![Action::new(ActKind::Pass).label("Don't block")];
                for &c in &blockers {
                    opts.push(Action::new(ActKind::Block).card(c).label(self.lbl(|| format!("Block with {}", self.describe(c)))));
                }
                let prompt = self.lbl(|| format!("{} ({}) attacks {}", self.name(attacker), self.power(attacker), self.describe(tgt)));
                let a = self.choose(o, DecisionKind::Block, opts, &prompt)?;
                if a.kind == ActKind::Block {
                    let blocker = a.card.unwrap();
                    self.cm(blocker).rested = true;
                    let inc = self.c(blocker).incarnation;
                    let b = self.s.battle.as_mut().unwrap();
                    b.target = blocker;
                    b.target_inc = inc;
                    let bn = self.name(blocker);
                    self.log(|| format!("{bn} blocks"));
                    self.fire(Timing::OnBlock, &[blocker]);
                    self.resolve_triggers()?;
                }
            }
        }
        if self.battle_interrupted() {
            return Ok(()); // 7-1-2-3
        }

        // Counter Step
        self.s.phase = Phase::Counter;
        loop {
            let tgt = self.s.battle.as_ref().unwrap().target;
            let mut opts = vec![Action::new(ActKind::Pass).label("No more counters")];
            for c in self.ps(o).hand.to_vec() {
                let d = self.d(c);
                if d.category == Category::Character && self.counter_value(c) > 0 {
                    // 7-1-3-1-1: boost the Leader or any 1 Character, the battling card first
                    let mut targets = vec![tgt];
                    targets.extend(self.leader_and_characters(o).into_iter().filter(|&t| t != tgt));
                    for t in targets {
                        opts.push(Action::new(ActKind::Counter).card(c).target(t).label(self.lbl(|| format!(
                            "Counter +{} on {} with {}",
                            self.counter_value(c),
                            self.name(t),
                            d.name
                        ))));
                    }
                } else if d.category == Category::Event && self.cost(c) <= self.ps(o).don_active as i32 {
                    if let Some(i) = d.first_effect(Timing::Counter) {
                        if self.can_activate(c, i) {
                            opts.push(Action::new(ActKind::Event).card(c).label(self.lbl(|| format!("Activate {}", self.describe(c)))));
                        }
                    }
                }
            }
            let prompt = self.lbl(|| format!("{} ({}) vs {}", self.name(attacker), self.power(attacker), self.describe(tgt)));
            let a = self.choose(o, DecisionKind::Counter, opts, &prompt)?;
            if a.kind == ActKind::Pass {
                break;
            }
            let card = a.card.unwrap();
            if a.kind == ActKind::Counter {
                let (value, boosted) = (self.counter_value(card), a.target.unwrap());
                self.mv(card, Zone::Trash);
                self.add_power(boosted, value, Until::Battle, Some(o));
                let (bn, cn) = (self.name(boosted), self.name(card));
                self.log(|| format!("Player {o} counters +{value} on {bn} with {cn}"));
            } else {
                self.pay_card_cost(card);
                self.activate_event(card, Timing::Counter)?;
            }
            self.resolve_triggers()?;
            if self.battle_interrupted() {
                return Ok(()); // 7-1-3-1-3
            }
        }

        // Damage Step
        self.s.phase = Phase::Damage;
        let tgt = self.s.battle.as_ref().unwrap().target;
        let (atk_power, tgt_power) = (self.power(attacker), self.power(tgt));
        if atk_power >= tgt_power {
            if self.d(tgt).category == Category::Leader {
                let dmg = if self.has_keyword(attacker, Kw::DoubleAttack) { 2 } else { 1 };
                let banish = self.has_keyword(attacker, Kw::Banish);
                self.deal_damage(o, dmg, banish)?;
            } else {
                self.ko(tgt, None, true, true)?;
            }
        } else {
            self.log(|| format!("Attack fails ({atk_power} < {tgt_power})"));
        }
        self.resolve_triggers() // 8-6-2: after all damage processing
    }
}

/// Everything `player` can see, as numbers (for checking a reshuffle didn't change it).
fn public_view(s: &State, pending: Option<&Decision>, player: u8) -> Vec<i64> {
    let mut v: Vec<i64> = vec![s.turn_number as i64, s.turn_player as i64, s.phase as i64];
    if let Some(b) = &s.battle {
        v.extend([b.attacker as i64, b.target as i64]);
    }
    let card = |v: &mut Vec<i64>, u: Uid| {
        let c = &s.cards[u as usize];
        v.extend([u as i64, c.def as i64, c.rested as i64, c.don as i64, c.mods.len() as i64]);
    };
    for (q, ps) in s.players.iter().enumerate() {
        v.push(-1);
        for u in ps.field() {
            card(&mut v, u);
        }
        v.push(-2);
        v.extend(ps.trash.iter().map(|&u| s.cards[u as usize].def as i64));
        v.push(-3);
        v.extend(ps.life.iter().map(|&u| if s.cards[u as usize].face_up { s.cards[u as usize].def as i64 } else { -9 }));
        v.push(-4);
        if q as u8 == player {
            v.extend(ps.hand.iter().map(|&u| s.cards[u as usize].def as i64));
        } else {
            v.push(ps.hand.len() as i64);
        }
        v.extend([ps.deck.len() as i64, ps.don_deck as i64, ps.don_active as i64, ps.don_rested as i64]);
    }
    if let Some(d) = pending {
        v.extend([-5, d.player as i64, d.kind as i64]);
        for o in &d.options {
            let n = |x: Option<Uid>| x.map_or(-1, |u| u as i64);
            v.extend([o.kind as i64, n(o.card), n(o.target), o.index.map_or(-1, |i| i as i64)]);
        }
    }
    v
}

/// What moving a card needs from a zone's list, whatever its inline size.
pub trait PileOps {
    fn remove_uid(&mut self, u: Uid);
    fn put(&mut self, u: Uid, bottom: bool);
}

impl<const N: usize> PileOps for Pile<N> {
    fn remove_uid(&mut self, u: Uid) {
        if let Some(i) = self.iter().position(|&x| x == u) {
            self.remove(i);
        }
    }
    fn put(&mut self, u: Uid, bottom: bool) {
        if bottom {
            self.insert(0, u);
        } else {
            self.push(u);
        }
    }
}

pub fn remove_first(v: &mut Vec<Uid>, u: Uid) {
    if let Some(i) = v.iter().position(|&x| x == u) {
        v.remove(i);
    }
}

// ---------------------------------------------------------------- the game

/// A game in progress: the state at the pending decision, plus what is needed to continue it.
#[derive(Clone)]
pub struct Game {
    pub db: Arc<Db>,
    pub state: State,
    checkpoint: State,
    seg: Seg,
    answers: Vec<Action>,
    pub log: Vec<String>,
    seg_log: usize,
    pub logging: bool,
    /// Option labels and prompts; off saves time in search.
    pub labels: bool,
    pub history: Vec<(u8, Action)>,
    pub pending: Option<Decision>,
    pub winner: Option<u8>,
    pub over: bool,
}

/// A deck as the Python side builds it: the Leader and the 50 cards, in order.
pub struct DeckList<'a> {
    pub leader: &'a str,
    pub cards: Vec<&'a str>,
}

impl Game {
    pub fn new(db: Arc<Db>, decks: [DeckList; 2], seed: u64) -> Result<Game, String> {
        let mut state = State {
            rng: PyRandom::new(seed),
            players: [PlayerState::new(), PlayerState::new()],
            cards: vec![],
            turn_number: 0,
            turn_player: 0,
            first_player: 0,
            phase: Phase::Setup,
            battle: None,
            queue: vec![],
            resolving_batch: None,
        };
        let mut log = vec![];
        {
            let mut e = Eng {
                s: &mut state,
                db: &db,
                answers: &[],
                pos: 0,
                log: &mut log,
                logging: false,
                labels: false,
                in_static_cost: Cell::new(false),
            };
            for (i, deck) in decks.iter().enumerate() {
                let i = i as u8;
                let leader = db.index(deck.leader)?;
                e.create_card(leader, i, Zone::Leader);
                for id in &deck.cards {
                    e.create_card(db.index(id)?, i, Zone::Deck);
                }
                let mut d = std::mem::take(&mut e.psm(i).deck);
                e.s.rng.shuffle(&mut d);
                e.psm(i).deck = d;
            }
        }
        let mut g = Game {
            db,
            checkpoint: state.clone(),
            state,
            seg: Seg::Start,
            answers: vec![],
            log,
            seg_log: 0,
            logging: true,
            labels: true,
            history: vec![],
            pending: None,
            winner: None,
            over: false,
        };
        g.advance(true);
        Ok(g)
    }

    /// Answer the pending decision with option `i`.
    pub fn step(&mut self, i: usize) -> Result<(), String> {
        let d = self.pending.as_ref().ok_or("the game is over")?;
        let a = d.options.get(i).ok_or_else(|| format!("no option {i} for {}", d.kind.as_str()))?.clone();
        // Only the Main Phase decision that opens a segment comes before any change to the
        // state. (When "End turn" is the only option it's taken without asking, and the first
        // question is a later one, after the End Phase has started.)
        let opening = d.kind == DecisionKind::Main && self.answers.is_empty();
        self.history.push((d.player, a.clone()));
        self.answers.push(a);
        let first_of_main = self.seg == Seg::Main && opening;
        if !self.advance(first_of_main) {
            panic!("replaying the current segment diverged: the engine is not deterministic");
        }
        Ok(())
    }

    /// Run on from the checkpoint with the answers so far. False if a replayed answer is no
    /// longer an option, which only happens after hidden cards were reshuffled.
    ///
    /// With `in_place`, the current state is known to equal the checkpoint (the answer was the
    /// segment's first, asked before anything changed), so it runs on without a copy.
    fn advance(&mut self, in_place: bool) -> bool {
        let mut in_place = in_place;
        loop {
            if !in_place {
                self.state.clone_from(&self.checkpoint);
                self.log.truncate(self.seg_log);
            }
            in_place = false;
            let r = {
                let mut e = Eng {
                    s: &mut self.state,
                    db: &self.db,
                    answers: &self.answers,
                    pos: 0,
                    log: &mut self.log,
                    logging: self.logging,
                    labels: self.labels,
                    in_static_cost: Cell::new(false),
                };
                let r = e.segment(self.seg);
                debug_assert!(r.is_ok() || e.pos == self.answers.len());
                r
            };
            match r {
                Ok(next) => {
                    // a segment boundary: the stack is empty, so the state alone can resume
                    self.checkpoint.clone_from(&self.state);
                    self.seg = next;
                    self.answers.clear();
                    self.seg_log = self.log.len();
                    in_place = true;
                }
                Err(Stop::Ask(d)) => {
                    self.pending = Some(d);
                    return true;
                }
                Err(Stop::Over(w)) => {
                    self.finish(w, None);
                    return true;
                }
                Err(Stop::Diverged) => return false,
            }
        }
    }

    /// A copy for search: no log, and no log lines, labels or prompts from here on.
    pub fn fork(&self) -> Game {
        Game {
            db: self.db.clone(),
            state: self.state.clone(),
            checkpoint: self.checkpoint.clone(),
            seg: self.seg,
            answers: self.answers.clone(),
            log: vec![],
            seg_log: 0,
            logging: false,
            labels: false,
            history: vec![],
            pending: self.pending.clone(),
            winner: self.winner,
            over: self.over,
        }
    }

    /// For search with hidden information: reshuffle the cards `player` can't see among the
    /// places they are in, each owner's cards among themselves (the opponent's hand, deck and
    /// face-down Life; your own deck and face-down Life). What `player` knows from earlier
    /// (cards they looked at and put back) is not tracked, and future random events keep the
    /// true random state.
    ///
    /// The shuffle is applied at the segment's checkpoint, to cards hidden both there and now,
    /// and the segment is replayed. If a replayed answer stops being an option the game is left
    /// unchanged and this returns false; try another seed.
    pub fn determinize(&mut self, player: u8, seed: u64) -> bool {
        if self.over {
            return false;
        }
        let hidden = |s: &State| -> Vec<bool> {
            let mut h = vec![false; s.cards.len()];
            for (p, ps) in s.players.iter().enumerate() {
                let mine = p as u8 == player;
                let mut zones: Vec<&[Uid]> = vec![&ps.deck];
                if !mine {
                    zones.push(&ps.hand);
                }
                for z in zones {
                    for &u in z {
                        h[u as usize] = true;
                    }
                }
                for &u in &ps.life {
                    if !s.cards[u as usize].face_up {
                        h[u as usize] = true;
                    }
                }
            }
            h
        };
        let (now, then) = (hidden(&self.state), hidden(&self.checkpoint));
        let saved = (self.checkpoint.clone(), self.state.clone(), self.pending.clone(), self.log.len());
        let mut rng = PyRandom::new(seed);
        for owner in 0..2u8 {
            let uids: Vec<usize> = (0..now.len())
                .filter(|&u| now[u] && then[u] && self.checkpoint.cards[u].owner == owner)
                .collect();
            let mut defs: Vec<u16> = uids.iter().map(|&u| self.checkpoint.cards[u].def).collect();
            rng.shuffle(&mut defs);
            for (&u, d) in uids.iter().zip(defs) {
                self.checkpoint.cards[u].def = d;
            }
        }
        // The replay must show `player` exactly what they saw: a reshuffled card can change
        // what a public effect does (a revealed top card that stays in the deck, say).
        if self.advance(false) && self.public_view(player) == public_view(&saved.1, saved.2.as_ref(), player) {
            return true;
        }
        let (ck, st, pending, log_len) = saved;
        self.checkpoint = ck;
        self.state = st;
        self.pending = pending;
        self.log.truncate(log_len);
        false
    }

    fn finish(&mut self, winner: Option<u8>, msg: Option<String>) {
        self.winner = winner;
        self.over = true;
        self.pending = None;
        if self.logging {
            let msg = msg.unwrap_or_else(|| match winner {
                None => "Game over: draw".into(),
                Some(w) => format!("Game over: player {w} wins"),
            });
            self.log.push(format!("[T{}] {}", self.state.turn_number, msg));
        }
    }

    /// 1-2-3: a player may concede at any time and loses immediately.
    pub fn concede(&mut self, p: u8) -> Result<(), String> {
        if self.over {
            return Err("the game is over".into());
        }
        if self.state.battle.is_some() {
            // closing the Python generator runs the battle's `finally`
            let mut log = vec![];
            let mut e = self.eng(&mut log);
            e.end_battle();
        }
        self.state.resolving_batch = None;
        self.finish(Some(1 - p), Some(format!("Player {p} concedes. Game over: player {} wins", 1 - p)));
        Ok(())
    }

    fn public_view(&self, player: u8) -> Vec<i64> {
        public_view(&self.state, self.pending.as_ref(), player)
    }

    /// Read-only engine access to the current state (power, cost, describe, ...).
    pub fn eng<'a>(&'a mut self, log: &'a mut Vec<String>) -> Eng<'a> {
        Eng {
            s: &mut self.state,
            db: &self.db,
            answers: &[],
            pos: 0,
            log,
            logging: false,
            labels: true,
            in_static_cost: Cell::new(false),
        }
    }

    pub fn view<T>(&mut self, f: impl FnOnce(&Eng) -> T) -> T {
        let mut log = vec![];
        let e = self.eng(&mut log);
        f(&e)
    }
}
