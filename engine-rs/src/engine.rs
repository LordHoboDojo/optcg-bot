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
/// [Once Per Turn] bookkeeping for replacement effects, apart from effect indexes.
const REPLACEMENT_KEY: usize = 10_000;

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
    /// the turn this player can't add Life cards to their hand with their own effects
    pub no_life_to_hand_turn: i32,
    /// DON!! to set active at the end of this turn
    pub don_active_at_end: u32,
    /// how many DON!! were returned to the DON!! deck last time some were
    pub don_just_returned: u32,
    /// (turn, base cost): this turn Characters with that base cost or more can't be played
    pub no_play_cost: (i32, i32),
    /// the last turn one of this player's Characters was K.O.'d
    pub ko_turn: i32,
    /// "the next time you play ... this turn, the cost will be reduced by N": (turn, which cards, N)
    pub next_play_discount: Option<(i32, crate::gen::Pred, i32)>,
    /// rested DON!! that stay rested in the next Refresh Phase
    pub don_skip_refresh: u32,
    /// turn-long restrictions, by the turn they apply to
    pub no_draw_turn: i32,
    pub no_hand_play_turn: i32,
    pub no_don_active_by_char_turn: i32,
    pub battle_ko_shield_turn: i32,
    /// this player's [On Play] effects are negated through this turn
    pub on_play_off_until: i32,
    pub extra_turn: bool,
    /// (turn, highest base cost) of the Events activated
    pub event_cost: (i32, i32),
    /// the last turn a card was trashed from this hand by an effect
    pub hand_trashed_turn: i32,
    /// active DON!! to rest at the start of the next Main Phase
    pub rest_don_at_main: u32,
    /// the last turn this player's Leader battled a Character
    pub leader_battled_char_turn: i32,
    /// the turn this player can't attack the opponent's Leader
    pub no_leader_attack_turn: i32,
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
            no_life_to_hand_turn: -1,
            don_active_at_end: 0,
            don_just_returned: 0,
            no_play_cost: (-1, 0),
            ko_turn: -1,
            next_play_discount: None,
            don_skip_refresh: 0,
            no_draw_turn: -1,
            no_hand_play_turn: -1,
            no_don_active_by_char_turn: -1,
            battle_ko_shield_turn: -1,
            on_play_off_until: -1,
            extra_turn: false,
            event_cost: (-1, -1),
            hand_trashed_turn: -1,
            rest_don_at_main: 0,
            leader_battled_char_turn: -1,
            no_leader_attack_turn: -1,
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
    /// the last K.O.: (owner of the card, whose effect or None, in battle)
    pub ko_ctx: Option<(u8, Option<u8>, bool, Uid)>,
    /// the last card removed from the field by an effect: (card, its owner, whose effect)
    pub removed_ctx: Option<(Uid, u8, Option<u8>)>,
    /// the last Character played: (card, the zone it was played from)
    pub played_ctx: Option<(Uid, Option<Zone>)>,
    /// who returned the last DON!! to a DON!! deck (an effect's player)
    pub don_return_by: Option<u8>,
    /// actions for the end of the current battle: (card, incarnation, what, whose effect)
    pub battle_end: Vec<(Uid, u32, LaterVerb, u8)>,
    /// where the last card that left a Life area went
    pub life_left_to: Option<Zone>,
    /// the last time cards were trashed from a hand by an effect: (how many, whose effect's card)
    pub hand_trashed_ctx: (usize, Option<Uid>),
    /// the last Character played was played by an effect of this card
    pub played_by: Option<Uid>,
    /// the last card rested by an effect was rested by an effect of this card
    pub rested_by: Option<Uid>,
    /// "... at the end of this turn"
    pub delayed: Vec<Delayed>,
}

/// Something an effect does at the end of the turn it resolved in.
#[derive(Clone, Debug)]
pub struct Delayed {
    pub turn: i32,
    pub player: u8,
    pub kind: DelayedKind,
}

#[derive(Clone, Debug)]
pub enum DelayedKind {
    /// (card, its incarnation then, what happens)
    Card(Uid, u32, LaterVerb),
    DeckToLife(usize),
    AddDonActive(u32),
    /// trash 1 of your Characters that fit (chosen then)
    TrashOne(crate::gen::Pred),
    /// set all of your Characters that fit as active
    SetActiveAll(crate::gen::Pred),
    /// return DON!! from your field until you have as many as your opponent
    DonMatch,
    /// up to 1 rested Character that fits won't become active in its owner's next Refresh Phase
    NoRefreshOne(crate::gen::Pred),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LaterVerb {
    Trash,
    ToHand,
    ToDeckBottom,
    SetActive,
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
    /// the cards the last general action chose ("it"), and whether it did anything (gen.rs)
    pub last: Vec<Uid>,
    pub did: bool,
    /// how many cards the last general action took ("for every card trashed")
    pub count: usize,
    /// "Choose a cost and reveal ...": the cost chosen
    pub guess: Option<i32>,
    /// the card whose effect is resolving
    pub cur_src: Option<Uid>,
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
        self.last = chosen.clone(); // what "it" / "that Character" means in later sentences
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
        if self.c(u).mods.iter().any(|m| m.kind == ModKind::Negated) {
            return true;
        }
        let c = self.c(u);
        if !matches!(c.zone, Some(Zone::Leader) | Some(Zone::Character)) {
            return false;
        }
        // "Your Leader and all of your Characters that do not have a type including X have their effects negated"
        self.ps(c.owner).characters.iter().any(|&s| {
            s != u
                && self.d(s).st.negate_own_except.as_ref().map_or(false, |t| !self.d(u).type_includes(t))
                && !self.c(s).mods.iter().any(|m| m.kind == ModKind::Negated)
        })
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
            for (cond, n, to) in &self.d(src).st.gen_base_power {
                if self.target_ok(to, src, u) && self.eval(cond, src) {
                    let v = self.num(n, src);
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
        if let Some(i) = c.mods.iter().rposition(|m| matches!(m.kind, ModKind::PowerSet(_))) {
            // "set the power to 0": then only what was added since counts
            let ModKind::PowerSet(v) = c.mods[i].kind else { unreachable!() };
            p = v + c.mods[i + 1..].iter().map(|m| if let ModKind::Power(x) = m.kind { x } else { 0 }).sum::<i32>();
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
                v -= self.hand_discount(u);
            }
            self.in_static_cost.set(false);
        }
        if let Some(i) = c.mods.iter().rposition(|m| matches!(m.kind, ModKind::CostSet(_))) {
            let ModKind::CostSet(x) = c.mods[i].kind else { unreachable!() };
            v = x + c.mods[i + 1..].iter().map(|m| if let ModKind::Cost(y) = m.kind { y } else { 0 }).sum::<i32>();
        }
        v.max(0)
    }

    /// Cost reductions for playing this card from hand: permanent ones from the owner's cards,
    /// and "the next time you play ..." this turn.
    fn hand_discount(&self, u: Uid) -> i32 {
        let owner = self.owner(u);
        let mut v = 0;
        for src in self.ps(owner).field_iter() {
            if self.negated(src) {
                continue;
            }
            for (cond, pred, n) in &self.d(src).st.hand_discount {
                if self.eval(cond, src) && self.pred_ok(pred, u, src) {
                    v += n;
                }
            }
        }
        let ps = self.ps(owner);
        if let Some((turn, pred, n)) = &ps.next_play_discount {
            if *turn == self.s.turn_number && self.pred_ok(pred, u, u) {
                v += n;
            }
        }
        v
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
            if self.c(u).zone == Some(Zone::Hand) && self.owner(src) == self.owner(u) {
                for (cond, pred, v) in &self.d(src).st.gen_counter {
                    if self.eval(cond, src) && self.pred_ok(pred, u, src) {
                        best = best.max(*v);
                    }
                }
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
            revealed: false,
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
        let left_life = old == Some(Zone::Life) && zone != Some(Zone::Life);
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
        if left_life {
            self.s.life_left_to = zone;
            let (mine, theirs) = (self.ps(owner).field(), self.ps(1 - owner).field());
            self.fire(Timing::LifeLeft, &mine);
            self.fire(Timing::OppLifeLeft, &theirs);
            if self.ps(owner).life.is_empty() {
                self.fire(Timing::LifeZero, &mine);
            }
        }
    }

    pub fn mv(&mut self, u: Uid, zone: Zone) {
        self.move_card(u, Some(zone), false, false);
    }

    // ------------------------------------------------------------ effect helpers

    pub fn draw(&mut self, p: u8, n: usize) {
        let mut drew = 0;
        for _ in 0..n {
            match self.ps(p).deck.last() {
                Some(&top) => self.mv(top, Zone::Hand),
                None => break,
            }
            drew += 1;
        }
        if drew > 0 && !matches!(self.s.phase, Phase::Draw | Phase::Setup) {
            let field = self.ps(p).field();
            self.fire(Timing::Drew, &field);
        }
    }

    /// Draws by effects, unless "you cannot draw cards using your own effects" this turn.
    pub fn effect_draw(&mut self, p: u8, n: usize, by: u8) {
        if p == by && self.ps(p).no_draw_turn == self.s.turn_number {
            return;
        }
        self.draw(p, n);
    }

    pub fn add_mod_pub(&mut self, u: Uid, kind: ModKind, until: Until, by: Option<u8>) {
        self.add_mod(u, kind, until, by);
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
        self.hand_trashed(p, cards.len());
        Ok(cards)
    }

    /// "When a card is trashed from your hand by an effect"
    pub fn hand_trashed(&mut self, p: u8, n: usize) {
        if n > 0 {
            self.s.hand_trashed_ctx = (n, self.cur_src);
            let t = self.s.turn_number;
            self.psm(p).hand_trashed_turn = t;
            let field = self.ps(p).field();
            self.fire(Timing::HandTrashed, &field);
        }
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
        if n > 0 && matches!(self.c(target).zone, Some(Zone::Leader) | Some(Zone::Character)) {
            let field = self.ps(self.owner(target)).field();
            self.fire(Timing::DonGiven, &field);
        }
        n
    }

    /// "When ... is removed from the field by ... effect" triggers.
    fn removed(&mut self, card: Uid, by: Option<u8>) {
        self.s.removed_ctx = Some((card, self.owner(card), by));
        let field = self.ordered_field();
        self.fire(Timing::Removed, &field);
    }

    /// Offer the owner's replacement effects for `card` leaving the field. True if one was used.
    fn replaced(&mut self, card: Uid, by: Option<u8>, ko: bool, in_battle: bool) -> R<bool> {
        let owner = self.owner(card);
        if ko && in_battle && self.ps(owner).battle_ko_shield_turn == self.s.turn_number && !self.ps(owner).hand.is_empty() {
            // "If any of your Characters would be K.O.'d in battle during this turn, you may trash 1 card ... instead"
            let n = self.name(card);
            if self.confirm(owner, &format!("Trash 1 card from your hand instead of {n} being K.O.'d?"))? {
                self.trash_from_hand(owner, 1)?;
                return Ok(true);
            }
        }
        for src in self.ps(owner).field() {
            if self.negated(src) {
                continue;
            }
            for (j, r) in self.d(src).st.replacements.iter().enumerate() {
                let key = REPLACEMENT_KEY + j;
                if r.once_per_turn && self.c(src).opt_turn(key) == Some(self.s.turn_number) {
                    continue;
                }
                if !((r.applies)(self, src, card, by, ko) && (r.payable)(self, src)) {
                    continue;
                }
                if self.confirm(owner, &format!("{}: {} instead?", self.name(src), r.text))? {
                    if r.once_per_turn {
                        let t = self.s.turn_number;
                        self.cm(src).set_opt(key, t);
                    }
                    let (s, c) = (self.name(src), self.name(card));
                    self.log(|| format!("{s} replaces the removal of {c}"));
                    (r.pay)(self, src, card)?;
                    return Ok(true);
                }
            }
            for (j, (r, text, opt)) in self.d(src).st.gen_replacements.iter().enumerate() {
                let key = REPLACEMENT_KEY + 100 + j;
                if *opt && self.c(src).opt_turn(key) == Some(self.s.turn_number) {
                    continue;
                }
                if !self.rep_applies(r, src, card, by, ko, in_battle) {
                    continue;
                }
                if !r.optional || self.confirm(owner, &format!("{}: {}?", self.name(src), text))? {
                    if *opt {
                        let t = self.s.turn_number;
                        self.cm(src).set_opt(key, t);
                    }
                    let (s, c) = (self.name(src), self.name(card));
                    self.log(|| format!("{s} replaces the removal of {c}"));
                    self.rep_pay(r, src, card)?;
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }

    /// Offer `player`'s "if you would take damage ... instead" effects for 1 point of damage.
    fn damage_replaced(&mut self, player: u8) -> R<bool> {
        for src in self.ps(player).field() {
            if self.negated(src) {
                continue;
            }
            for r in &self.d(src).st.damage_replacements {
                if !((r.applies)(self, src, player) && (r.payable)(self, src)) {
                    continue;
                }
                if self.confirm(player, &format!("{}: {} instead of taking damage?", self.name(src), r.text))? {
                    let n = self.name(src);
                    self.log(|| format!("{n} replaces 1 damage"));
                    (r.pay)(self, src)?;
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
        if by == Some(1 - self.owner(card)) && self.has_keyword(card, Kw::NoRemoveByOpp) {
            return Ok(false);
        }
        if self.replaced(card, by, false, false)? {
            return Ok(false);
        }
        self.move_card(card, Some(zone), bottom, face_up);
        self.removed(card, by);
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
                Some(c) => {
                    self.cm(c).rested = true;
                    self.fire(Timing::BecameRested, &[c]);
                }
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
            if self.has_keyword(card, Kw::NoKoInBattle) {
                return true;
            }
            // "cannot be K.O.'d in battle by <Slash> attribute cards": the card it battles
            let other = self.s.battle.as_ref().map(|b| if b.attacker == card { b.target } else { b.attacker });
            return other.map_or(false, |o| {
                self.static_sources().any(|src| {
                    self.d(src).st.no_ko_battle_by.iter().any(|(cond, to, pred)| {
                        self.target_ok(to, src, card) && self.eval(cond, src) && self.pred_ok(pred, o, src)
                    })
                })
            });
        }
        if self.has_keyword(card, Kw::NoKoByEffects) {
            return true;
        }
        if by == Some(1 - self.owner(card)) && self.has_keyword(card, Kw::NoRemoveByOpp) {
            return true;
        }
        if let Some(s) = self.cur_src {
            // "cannot be K.O.'d by effects of your opponent's Characters with 5000 base power or less"
            let hit = self.static_sources().any(|src| {
                self.d(src).st.no_ko_effect_by.iter().any(|(cond, to, pred)| {
                    self.target_ok(to, src, card) && self.eval(cond, src) && self.pred_ok(pred, s, src)
                })
            });
            if hit {
                return true;
            }
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
        if replaceable && self.replaced(card, by, true, in_battle)? {
            return Ok(());
        }
        self.s.ko_ctx = Some((self.owner(card), by, in_battle, card));
        let ready: Vec<usize> =
            self.d(card).effect_indexes(Timing::OnKo).filter(|&i| self.can_activate(card, i)).collect();
        let field: Vec<Uid> = self.ordered_field().into_iter().filter(|&c| c != card).collect();
        self.fire(Timing::CharKo, &field);
        // "when one of your Characters ... is K.O.'d": judged while the card is still on the field
        let owner = self.owner(card);
        let mut watchers = vec![];
        for src in self.ps(owner).field() {
            if src == card {
                continue;
            }
            for i in self.d(src).effect_indexes(Timing::AllyKo) {
                let f = self.d(src).effects[i].ko_filter.expect("AllyKo effect without ko_filter");
                if self.can_activate(src, i) && f(self, src, card) {
                    watchers.push((src, i));
                }
            }
        }
        self.log(|| format!("{name} is K.O.'d"));
        let mut paid = vec![];
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
        let t = self.s.turn_number;
        self.psm(owner).ko_turn = t;
        if !in_battle && by.is_some() {
            self.removed(card, by); // a K.O. by an effect removes it from the field
        }
        for i in paid {
            let p = Pending { src: card, idx: i, inc: self.c(card).incarnation, batch: self.next_batch(), activated: true };
            self.s.queue.push(p);
        }
        for (src, i) in watchers {
            let p = Pending { src, idx: i, inc: self.c(src).incarnation, batch: self.next_batch(), activated: false };
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
        if amount > 0 {
            let field = self.ps(p).field();
            self.fire(Timing::TookDamage, &field);
        }
        for _ in 0..amount {
            // one point at a time (4-6-2-2)
            if self.damage_replaced(p)? {
                continue;
            }
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
                let field = self.ordered_field();
                self.fire(Timing::TriggerUsed, &field);
                let other = self.ps(1 - p).field();
                self.fire(Timing::OppTrigger, &other);
                self.log(|| format!("Player {p} activates [Trigger] of {name}"));
                self.resolve(card, trig[0])?;
                if self.c(card).zone.is_none() {
                    self.mv(card, Zone::Trash);
                } else if self.c(card).zone == Some(Zone::Hand) {
                    self.cm(card).revealed = true; // e.g. "add this card to your hand": everyone saw it
                }
            } else if self.c(card).face_up && self.d(self.ps(p).leader).st.faceup_life_to_deck {
                // "Your face-up Life cards are placed at the bottom of your deck instead"
                self.move_card(card, Some(Zone::Deck), true, false);
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
            self.fire(Timing::BecameRested, &[src]);
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

    fn run_delayed(&mut self, d: &Delayed) -> R<()> {
        match d.kind.clone() {
            DelayedKind::Card(u, inc, v) => {
                if self.c(u).incarnation != inc {
                    return Ok(()); // it left the field (or moved) since
                }
                match v {
                    LaterVerb::Trash => {
                        self.remove_from_field(u, Zone::Trash, Some(d.player), false, false)?;
                    }
                    LaterVerb::ToHand => {
                        self.remove_from_field(u, Zone::Hand, Some(d.player), false, false)?;
                    }
                    LaterVerb::ToDeckBottom => {
                        self.remove_from_field(u, Zone::Deck, Some(d.player), true, false)?;
                    }
                    LaterVerb::SetActive => self.cm(u).rested = false,
                }
            }
            DelayedKind::DeckToLife(n) => {
                for _ in 0..n {
                    let Some(&top) = self.ps(d.player).deck.last() else { break };
                    self.mv(top, Zone::Life);
                }
            }
            DelayedKind::AddDonActive(n) => {
                self.add_don(d.player, n, false);
            }
            DelayedKind::TrashOne(pred) => {
                let me = d.player;
                let leader = self.ps(me).leader;
                let cands = self.characters(me).into_iter().filter(|&c| self.pred_ok(&pred, c, leader)).collect();
                for c in self.choose_cards(me, cands, 1, "Trash 1 at the end of the turn", true)? {
                    self.remove_from_field(c, Zone::Trash, Some(me), false, false)?;
                }
            }
            DelayedKind::SetActiveAll(pred) => {
                let leader = self.ps(d.player).leader;
                for c in self.characters(d.player) {
                    if self.pred_ok(&pred, c, leader) {
                        self.cm(c).rested = false;
                    }
                }
            }
            DelayedKind::DonMatch => {
                let (me, o) = (d.player, 1 - d.player);
                let extra = self.don_on_field(me).saturating_sub(self.don_on_field(o));
                if extra > 0 {
                    self.return_don(me, extra)?;
                }
            }
            DelayedKind::NoRefreshOne(pred) => {
                let me = d.player;
                let leader = self.ps(me).leader;
                let cands = (0..2u8)
                    .flat_map(|p| self.characters(p))
                    .filter(|&c| self.c(c).rested && self.pred_ok(&pred, c, leader))
                    .collect();
                for c in self.choose_cards(me, cands, 1, "Won't become active next Refresh Phase", false)? {
                    self.skip_next_refresh(c, me);
                }
            }
        }
        Ok(())
    }

    /// DON!! -X (8-3-1-6, 10-2-10).
    pub fn return_don(&mut self, p: u8, n: u32) -> R<()> {
        let mut returned = 0;
        let by = self.s.don_return_by.take().unwrap_or(p);
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
            returned += 1;
        }
        self.psm(p).don_just_returned = returned;
        self.s.don_return_by = Some(by);
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
        // "that Character" in a triggered effect: the card that set it off
        self.last = match eff.timing {
            Timing::MyPlay | Timing::OppPlay => self.s.played_ctx.map(|(c, _)| c).into_iter().collect(),
            Timing::CharKo => self.s.ko_ctx.map(|(_, _, _, c)| c).into_iter().collect(),
            Timing::Removed => self.s.removed_ctx.map(|(c, _, _)| c).into_iter().collect(),
            _ => vec![],
        };
        self.count = 0;
        self.guess = None;
        let outer = self.cur_src.replace(src);
        let r = self.run(&eff.body, src);
        self.cur_src = outer;
        r
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
        self.deck_out(false)
    }

    /// Defeat by an empty deck. Leaders can change it: "you win instead of losing", or "you
    /// lose at the end of the turn in which your deck becomes 0" (checked with `end_of_turn`).
    fn deck_out(&mut self, end_of_turn: bool) -> R<()> {
        for p in 0..2u8 {
            let st = &self.d(self.ps(p).leader).st;
            if self.ps(p).deck.is_empty() && st.deck_out_wins && !self.negated(self.ps(p).leader) {
                self.log(|| format!("Player {p}'s deck is reduced to 0: they win"));
                return Err(Stop::Over(Some(p)));
            }
        }
        let losers: Vec<u8> = (0..2u8)
            .filter(|&p| {
                let l = self.ps(p).leader;
                self.ps(p).deck.is_empty() && (end_of_turn || !(self.d(l).st.deck_out_at_end && !self.negated(l)))
            })
            .collect();
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
                    if !std::mem::take(&mut self.psm(p).extra_turn) {
                        self.s.turn_player = 1 - self.s.turn_player; // "take an extra turn after this one" keeps it
                    }
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
        for p in 0..2u8 {
            if let Some(n) = self.d(self.ps(p).leader).st.don_deck_size {
                self.psm(p).don_deck = n; // "your DON!! deck consists of 6 cards"
            }
        }
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
        for c in self.s.cards.iter_mut() {
            // "until the end of your next turn": from now on, until the end of this one
            for m in c.mods.iter_mut() {
                if m.until == Until::YourNextEnd && m.player == p {
                    m.until = Until::Turn;
                }
            }
        }
        let field = self.ordered_field();
        self.fire(Timing::StartOfTurn, &field); // 6-2-2
        self.resolve_triggers()?;
        for c in self.ps(p).field() {
            let don = self.c(c).don;
            self.psm(p).don_rested += don;
            self.cm(c).don = 0;
        }
        for c in self.ps(p).field() {
            if self.has_keyword(c, Kw::StayRested) {
                continue; // "do not become active in your and your opponent's Refresh Phases"
            }
            let card = self.cm(c);
            if card.mods.iter().any(|m| m.kind == ModKind::NoRefresh) {
                card.mods.retain(|m| m.kind != ModKind::NoRefresh);
            } else {
                card.rested = false;
            }
        }
        let ps = self.psm(p);
        let keep = std::mem::take(&mut ps.don_skip_refresh).min(ps.don_rested);
        ps.don_active += ps.don_rested - keep;
        ps.don_rested = keep;

        // Draw Phase (6-3)
        self.s.phase = Phase::Draw;
        if self.s.turn_number != 1 {
            self.draw(p, 1);
        }

        // DON!! Phase (6-4)
        self.s.phase = Phase::Don;
        let n = if self.s.turn_number == 1 { 1 } else { 2 };
        let had = self.don_on_field(p) > 0;
        let added = self.add_don(p, n, false);
        let l = self.ps(p).leader;
        if added > 0 && had && self.d(l).st.don_phase_to_leader && !self.negated(l) {
            self.give_don(p, l, 1, false); // "1 DON!! card placed during your DON!! Phase is given to your Leader"
        }
        let k = std::mem::take(&mut self.psm(p).rest_don_at_main).min(self.ps(p).don_active);
        if k > 0 {
            let ps = self.psm(p);
            ps.don_active -= k;
            ps.don_rested += k;
        }

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
        let t = self.s.turn_number;
        let due: Vec<Delayed> = self.s.delayed.iter().filter(|d| d.turn == t).cloned().collect();
        self.s.delayed.retain(|d| d.turn != t);
        for d in due {
            self.run_delayed(&d)?;
        }
        for q in [p, 1 - p] {
            // "set up to N of your DON!! cards as active at the end of this turn"
            let ps = self.psm(q);
            let n = std::mem::take(&mut ps.don_active_at_end).min(ps.don_rested);
            ps.don_rested -= n;
            ps.don_active += n;
        }
        let theirs = self.ps(1 - p).field();
        self.fire(Timing::EndOfOppTurn, &theirs);
        self.resolve_triggers()?;
        self.expire(Until::Turn, None);
        self.expire(Until::NextEnd, Some(1 - p)); // "until the end of your opponent's next End Phase"
        self.deck_out(true)
    }

    pub fn main_options(&self, p: u8) -> Vec<Action> {
        let ps = self.ps(p);
        let mut opts = vec![];
        for &c in &ps.hand {
            if ps.no_hand_play_turn == self.s.turn_number {
                break; // "you cannot play cards from your hand during this turn"
            }
            let cost = self.cost(c);
            if cost > ps.don_active as i32 {
                continue;
            }
            let d = self.d(c);
            if d.category == Category::Character && ps.no_character_play_turn == self.s.turn_number {
                continue;
            }
            if d.category == Category::Character && ps.no_play_cost.0 == self.s.turn_number && d.cost >= ps.no_play_cost.1 {
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
                if self.c(atk).rested || self.has_keyword(atk, Kw::CannotAttack) || self.has_keyword(atk, Kw::NoRest) {
                    continue;
                }
                if self.has_keyword(atk, Kw::AttackTax) && self.ps(p).hand.len() < 2 {
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
        let any = self.has_keyword(atk, Kw::AttackActive);
        targets.extend(opp.characters.iter().copied().filter(|&c| any || self.c(c).rested));
        let locks: Vec<&str> = self
            .static_sources()
            .filter(|&src| self.owner(src) != self.owner(atk))
            .filter(|&src| self.d(src).st.attack_lock.map_or(false, |f| f(self, src)) || self.has_keyword(src, Kw::Taunt))
            .map(|src| self.name(src))
            .collect();
        if !locks.is_empty() {
            // "your opponent cannot attack any card other than the Character [X]"
            targets.retain(|&c| self.d(c).category == Category::Character && locks.contains(&self.name(c)));
        }
        if self.ps(self.owner(atk)).no_leader_attack_turn == self.s.turn_number {
            targets.retain(|&c| self.d(c).category != Category::Leader);
        }
        if self.has_keyword(atk, Kw::NoAttackLowChars) {
            targets.retain(|&c| self.d(c).category != Category::Character || self.d(c).cost > 7);
        }
        if self.d(atk).category == Category::Character
            && self.c(atk).played_turn == self.s.turn_number
            && self.has_keyword(atk, Kw::NoLeaderFirstTurn)
        {
            targets.retain(|&c| self.d(c).category != Category::Leader);
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
                let used = self.ps(p).next_play_discount.as_ref().map_or(false, |(t, pred, _)| {
                    *t == self.s.turn_number && self.d(card).category == Category::Character && self.pred_ok(pred, card, card)
                });
                self.pay_card_cost(card);
                if used {
                    self.psm(p).next_play_discount = None;
                }
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
        let (t, c) = (self.s.turn_number, self.d(card).cost);
        let ec = &mut self.psm(owner).event_cost;
        *ec = if ec.0 == t { (t, ec.1.max(c)) } else { (t, c) };
        let (mine, theirs) = (self.ps(owner).field(), self.ps(1 - owner).field());
        self.fire(Timing::MyEvent, &mine);
        self.fire(Timing::OppEvent, &theirs);
        self.activate(card, idx)
    }

    fn play_character(&mut self, card: Uid, rested: bool) -> R<()> {
        let owner = self.owner(card);
        if self.ps(owner).no_character_play_turn == self.s.turn_number {
            return Ok(());
        }
        let (t, min) = self.ps(owner).no_play_cost;
        if t == self.s.turn_number && self.d(card).cost >= min {
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
        self.s.played_ctx = Some((card, self.c(card).zone));
        self.s.played_by = self.cur_src;
        let l = self.ps(owner).leader;
        let rested = rested || (self.d(l).st.play_rested && !self.negated(l));
        self.mv(card, Zone::Character);
        let t = self.s.turn_number;
        let c = self.cm(card);
        c.rested = rested;
        c.played_turn = t;
        let name = self.name(card);
        self.log(|| format!("Player {owner} plays {name}"));
        if self.on_play_allowed(owner) {
            self.fire(Timing::OnPlay, &[card]);
        }
        let mine: Vec<Uid> = self.ps(owner).field().into_iter().filter(|&c| c != card).collect();
        let theirs = self.ps(1 - owner).field();
        self.fire(Timing::MyPlay, &mine);
        self.fire(Timing::OppPlay, &theirs);
        Ok(())
    }

    /// "Your [On Play] effects are negated": by the Leader's rule or an opponent's effect.
    fn on_play_allowed(&self, p: u8) -> bool {
        let l = self.ps(p).leader;
        !(self.d(l).st.no_own_on_play && !self.negated(l)) && self.ps(p).on_play_off_until < self.s.turn_number
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
        if self.on_play_allowed(owner) {
            self.fire(Timing::OnPlay, &[card]);
        }
    }

    // ------------------------------------------------------------ battle (7)

    fn battle_interrupted(&self) -> bool {
        let b = self.s.battle.as_ref().unwrap();
        self.c(b.attacker).incarnation != b.attacker_inc || self.c(b.target).incarnation != b.target_inc
    }

    fn battle(&mut self, attacker: Uid, target: Uid) -> R<()> {
        for (a, b) in [(attacker, target), (target, attacker)] {
            if self.d(a).category == Category::Leader && self.d(b).category == Category::Character {
                let t = self.s.turn_number;
                let o = self.owner(a);
                self.psm(o).leader_battled_char_turn = t;
            }
        }
        self.s.battle = Some(Battle {
            attacker,
            target,
            attacker_inc: self.c(attacker).incarnation,
            target_inc: self.c(target).incarnation,
        });
        let r = self.battle_steps().and_then(|_| {
            // End of the Battle (7-1-5), also reached when a step was cut short
            self.s.phase = Phase::EndOfBattle;
            for (u, inc, v, by) in std::mem::take(&mut self.s.battle_end) {
                // "at the end of this battle, place this Character at the bottom of the owner's deck"
                if self.c(u).incarnation == inc {
                    match v {
                        LaterVerb::ToDeckBottom => {
                            self.remove_from_field(u, Zone::Deck, Some(by), true, false)?;
                        }
                        LaterVerb::Trash => {
                            self.remove_from_field(u, Zone::Trash, Some(by), false, false)?;
                        }
                        LaterVerb::ToHand => {
                            self.remove_from_field(u, Zone::Hand, Some(by), false, false)?;
                        }
                        LaterVerb::SetActive => self.cm(u).rested = false,
                    }
                }
            }
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
        if self.has_keyword(attacker, Kw::AttackTax) {
            let p = self.owner(attacker);
            self.trash_from_hand(p, 2)?; // "unless your opponent trashes 2 cards from their hand whenever they attack"
        }
        self.fire(Timing::BecameRested, &[attacker]);
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
                .filter(|&c| {
                    c != tgt && !self.c(c).rested && self.has_keyword(c, Kw::Blocker) && !self.has_keyword(c, Kw::NoBlocker) && !self.has_keyword(c, Kw::NoRest)
                })
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
                    self.fire(Timing::BecameRested, &[blocker]);
                    let theirs = self.ps(1 - o).field();
                    self.fire(Timing::OppBlock, &theirs);
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
                self.fire(Timing::DealtDamage, &[attacker]);
                let mine = self.ps(1 - o).field();
                self.fire(Timing::MyDamage, &mine);
            } else {
                self.ko(tgt, None, true, true)?;
                if self.c(tgt).zone != Some(Zone::Character) {
                    self.fire(Timing::BattleKo, &[attacker]);
                }
            }
        } else {
            self.log(|| format!("Attack fails ({atk_power} < {tgt_power})"));
        }
        self.resolve_triggers() // 8-6-2: after all damage processing
    }
}

/// Everything `player` can see, as numbers (for checking a reshuffle didn't change it).
/// Cards `player` can't see: both decks, the opponent's hand except revealed cards, face-down
/// Life. The cards in `player`'s own pending choice are seen.
fn hidden(s: &State, pending: Option<&Decision>, player: u8) -> Vec<bool> {
    let mut h = vec![false; s.cards.len()];
    for (p, ps) in s.players.iter().enumerate() {
        for &u in ps.deck.iter() {
            h[u as usize] = true;
        }
        if p as u8 != player {
            for &u in ps.hand.iter() {
                h[u as usize] = !s.cards[u as usize].revealed;
            }
        }
        for &u in ps.life.iter() {
            if !s.cards[u as usize].face_up {
                h[u as usize] = true;
            }
        }
    }
    if let Some(d) = pending.filter(|d| d.player == player) {
        for o in &d.options {
            for u in [o.card, o.target].into_iter().flatten() {
                h[u as usize] = false;
            }
        }
    }
    h
}

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
            ko_ctx: None,
            removed_ctx: None,
            played_ctx: None,
            don_return_by: None,
            battle_end: vec![],
            life_left_to: None,
            hand_trashed_ctx: (0, None),
            played_by: None,
            rested_by: None,
            delayed: vec![],
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
            last: vec![],
            did: false,
            count: 0,
            guess: None,
            cur_src: None,
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
            if !std::mem::take(&mut in_place) {
                self.state.clone_from(&self.checkpoint);
                self.log.truncate(self.seg_log);
            }
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
            last: vec![],
            did: false,
            count: 0,
            guess: None,
            cur_src: None,
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

    /// Change the state at the Main Phase decision that opens a segment (tests set up boards
    /// this way), then ask that decision again.
    pub fn arrange(&mut self, f: impl FnOnce(&mut Eng)) {
        let d = self.pending.as_ref().expect("the game is over");
        assert!(d.kind == DecisionKind::Main && self.answers.is_empty() && self.seg == Seg::Main);
        {
            let mut log = vec![];
            let mut e = self.eng(&mut log);
            f(&mut e);
        }
        self.checkpoint.clone_from(&self.state);
        self.advance(true);
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
    /// face-down Life; your own deck and face-down Life). Revealed hand cards and the cards in
    /// `player`'s own pending choice (a deck search, say) stay put. What `player` knows from earlier
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
        let (now, then) = (hidden(&self.state, self.pending.as_ref(), player), hidden(&self.checkpoint, None, player));
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

    /// A name for each pending option that means the same thing in every determinization
    /// `player` might search: (kind, card, target, index), where a card `player` can see is
    /// its uid and a hidden one is 1_000_000 + its card number. -1 for none.
    pub fn option_keys(&self, player: u8) -> Vec<(u8, i64, i64, i64)> {
        let Some(d) = &self.pending else { return vec![] };
        let h = hidden(&self.state, Some(d), player);
        let key = |u: Option<Uid>| match u {
            None => -1,
            Some(u) if h[u as usize] => 1_000_000 + self.state.cards[u as usize].def as i64,
            Some(u) => u as i64,
        };
        d.options.iter().map(|o| (o.kind as u8, key(o.card), key(o.target), o.index.map_or(-1, |i| i as i64))).collect()
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
            last: vec![],
            did: false,
            count: 0,
            guess: None,
            cur_src: None,
        }
    }

    pub fn view<T>(&mut self, f: impl FnOnce(&Eng) -> T) -> T {
        let mut log = vec![];
        let e = self.eng(&mut log);
        f(&e)
    }
}
