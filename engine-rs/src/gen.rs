//! General effects, built by the second compiler (compile2.rs) for card text the first one
//! (compile.rs, kept identical to Python) can't read. Instead of one template per wording, an
//! effect is a selection of cards ("up to 1 of your opponent's rested Characters with a cost of
//! 4 or less"), an action on them ("K.O.", "give -2000 power during this turn") and conditions
//! made of counts and card properties, so one piece covers many printed sentences.

use crate::cards::*;
use crate::effects::*;
use crate::engine::{Delayed, DelayedKind, Eng, LaterVerb, R};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Me,
    Opp,
    Both,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cmp {
    Le,
    Ge,
    Eq,
    Lt,
    Gt,
}

impl Cmp {
    pub fn ok(self, a: i32, b: i32) -> bool {
        match self {
            Cmp::Le => a <= b,
            Cmp::Ge => a >= b,
            Cmp::Eq => a == b,
            Cmp::Lt => a < b,
            Cmp::Gt => a > b,
        }
    }
}

/// A number in card text: printed, or counted in the game when the effect checks it.
#[derive(Clone, Debug)]
pub enum Num {
    K(i32),
    Life(Side),
    Hand(Side),
    Trash(Side),
    Deck(Side),
    /// DON!! on the field: cost area plus given (6-5-5)
    DonField(Side),
    DonActive(Side),
    DonRested(Side),
    DonGiven(Side),
    /// how many cards fit a selection
    Count(Box<Sel>),
    /// this card's power, cost, or DON!! given to it
    ThisPower,
    ThisDon,
    /// DON!! returned to your DON!! deck the last time some were
    DonJustReturned,
    /// (n / every) * times
    Per(Box<Num>, i32, i32),
    Sub(Box<Num>, Box<Num>),
    /// total cost of a side's Characters
    TotalCost(Side),
    /// how many turns `me` has had, this one included
    MyTurns,
    LeaderPower(Side),
    LeaderBase(Side),
    /// power of the card chosen earlier in the effect
    ItPower,
    /// power of the attacking card in the current battle
    AttackerPower,
    /// how many cards the last general action took
    Counted,
    /// DON!! given to / cost of the card chosen earlier
    ItDon,
    ItCost,
    /// how many different card names among the cards that fit
    DistinctNames(Box<Sel>),
    /// a if the condition holds, else b
    IfQ(Box<Q>, i32, i32),
    /// cards trashed from a hand by the last such effect
    HandTrashedCount,
    Sum(Box<Num>, Box<Num>),
}

/// What a card must be. Current power and cost unless `base`.
#[derive(Clone, Debug, Default)]
pub struct Pred {
    /// empty: Leader, Character or Stage on the field; any card elsewhere
    pub cats: Vec<Category>,
    pub names: Vec<String>,
    pub not_names: Vec<String>,
    pub types: Vec<String>,
    /// must have none of these types
    pub not_types: Vec<String>,
    pub type_inc: Vec<String>,
    pub colors: Vec<String>,
    pub multicolor: Option<bool>,
    pub attrs: Vec<String>,
    pub cost: Vec<(Cmp, Num, bool)>,
    pub power: Vec<(Cmp, Num, bool)>,
    pub counter: Option<(Cmp, i32)>,
    pub rested: Option<bool>,
    pub trigger: Option<bool>,
    /// no effect text printed (a "vanilla" card)
    pub vanilla: Option<bool>,
    pub has_don: Option<bool>,
    /// at least this many DON!! given
    pub don_min: Option<u32>,
    pub not_type_inc: Vec<String>,
    /// the card name includes this
    pub name_inc: Option<String>,
    /// a Life card that is face-up (or face-down)
    pub face_up: Option<bool>,
    /// has no effect with this timing ("without a [When Attacking] effect")
    pub without_timing: Option<Timing>,
    /// has none of these attributes ("without the <Special> attribute")
    pub not_attrs: Vec<String>,
    /// the cards chosen must all have different names
    pub distinct_names: bool,
    /// the opponent's cards only (of the effect's player)
    pub opp_only: bool,
    /// shares no color with / has the same name as the card chosen earlier ("it")
    pub not_color_of_it: bool,
    pub same_name_as_it: bool,
    pub not_this: bool,
    /// fits any of these as well as the rest ("[Monkey.D.Luffy] or red Event")
    pub any_of: Vec<Pred>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Place {
    Field,
    Hand,
    Trash,
    Life,
    /// the top N cards of Life
    LifeTop(usize),
    /// the top N cards of the deck
    DeckTop(usize),
    /// the whole deck
    Deck,
    /// hand, then trash
    HandTrash,
    /// the top or bottom card of Life (the player picks the end)
    LifeEnds,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Many {
    UpTo(usize),
    Exactly(usize),
    All,
}

#[derive(Clone, Debug)]
pub enum Sel {
    This,
    /// the cards the previous action of this effect chose ("it", "that Character")
    It,
    /// those of them still in a deck or Life ("place the rest at the bottom of your deck")
    Rest,
    Cards { side: Side, place: Place, many: Many, pred: Pred },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verb {
    Ko,
    Rest,
    SetActive,
    ToHand,
    /// `ends`: the owner picks top or bottom
    ToDeck { bottom: bool, ends: bool },
    Trash,
    /// to Life; `ends`: the owner picks top or bottom
    ToLife { face_up: bool, ends: bool, bottom: bool },
    Power(i32, Until),
    Cost(i32, Until),
    BasePower(i32, Until),
    Grant(Kw, Until),
    Negate(Until),
    Play { rested: bool },
    GiveDon { n: u32, rested: bool },
    NoRefresh,
    /// chosen only, so a later sentence can say "it" ("Choose 1 of your Characters.")
    Pick,
    Reveal,
    /// the controller picks: return to the owner's hand, or the bottom of the deck
    ToHandOrDeck,
    /// the controller picks: K.O. it or return it to the owner's hand
    KoOrToHand,
    /// at the end of this turn
    Later(LaterVerb),
    /// turn a Life card face-up / face-down
    Face(bool),
    /// the controller picks: K.O. it or rest it
    KoOrRest,
    /// the controller picks: play it, or add it to the top of their Life face-up
    PlayOrLife,
    /// +N power for each card the previous action took ("for every card trashed")
    PowerPer(i32, Until),
    PowerSet(i32, Until),
    CostSet(i32, Until),
    /// the attack's target becomes this card
    Redirect,
    /// give N of the opponent's DON!! (rested ones only, or any in their cost area) to this opponent's card
    GiveOppDon { n: u32, rested: bool },
    /// at the end of this battle
    AtBattleEnd(LaterVerb),
}

#[derive(Clone, Debug)]
pub struct Act {
    pub sel: Sel,
    pub verb: Verb,
    /// "your opponent chooses / trashes / places ..."
    pub opp_chooses: bool,
}

/// Actions that don't pick cards.
#[derive(Clone, Debug)]
pub enum Do {
    Draw(usize),
    OppDraw(usize),
    /// return N DON!! from `side`'s field to the DON!! deck (the owner picks which)
    ReturnDon { opp: bool, n: u32 },
    /// rest N of `side`'s active DON!! in the cost area
    RestDon { opp: bool, n: u32 },
    Shuffle,
    /// reveal the top card of your deck (it stays there): later sentences say "it"
    RevealTop,
    /// trash N from the top of the opponent's Life
    TrashOppLife(usize),
    /// put the top card of your deck at the top of your Life
    DeckToLife(usize),
    /// the player can't add Life cards to their hand with their own effects this turn
    NoLifeToHand,
    /// set up to N of your DON!! as active at the end of this turn
    DonActiveAtEnd(u32),
    Damage(u32),
    /// trash from the top of your Life until N are left
    TrashLifeUntil(usize),
    /// look at the top N, add up to K that fit to your hand (revealed), the rest go `rest`
    Look { n: usize, k: usize, pred: Pred, rest: LookRest, to: LookTo },
    /// turn all of your Life cards face-up or face-down
    TurnLife { up: bool },
    /// return N or more DON!! from your field (you pick how many)
    ReturnDonAtLeast(u32),
    /// look at the top card of your or your opponent's Life and put it at the top or bottom
    PeekLife,
    /// this turn you can't play Characters with this base cost or more
    NoPlayCost(i32),
    /// draw 0 to N (you pick)
    DrawUpTo(u32),
    /// draw until you have N in hand
    DrawTo(usize),
    DrawCount(Num),
    /// trash from hand until N are left (`both`: each player does)
    TrashTo { n: usize, both: bool },
    TakeDamage(u32),
    /// look at the top N and put them back on top in any order
    ReorderTop(usize),
    /// look at all of a Life area and put them back in any order
    ReorderLife { opp: bool },
    DeckToLifeLater(usize),
    AddDonLater(u32),
    SetAllDonActive,
    /// rest up to N of the opponent's active DON!! or Characters that fit
    RestDonOrChar { n: usize, pred: Pred },
    OppAddDon(u32),
    /// choose a cost and reveal the top card of the opponent's deck ("it")
    GuessCost,
    /// look at the top card of the opponent's deck (you learn it; nothing moves)
    LookOppTop,
    /// swap the base power of the two cards chosen before
    SwapBase(Until),
    /// this card's base power becomes N
    BasePowerFrom(Num, Until),
    /// activate the [Main] effect of an Event in your hand (it is trashed) or trash
    ActivateEvent { trash: bool, pred: Pred },
    /// "the next time you play ... from your hand during this turn, the cost will be reduced by N"
    NextPlayDiscount(Pred, i32),
    /// give 1 rested DON!! (either player's) to its owner's Leader or 1 of their Characters
    GiveDonToOwner { any_area: bool },
    /// "it" is the attacking card
    ItIsAttacker,
    /// trash as many from the top of your deck as the last action took
    MillCount,
    ShuffleOpp,
    /// look at all your Life, put N on top of your deck and the rest back in any order
    LifeToDeckTop(usize),
    OppReturnActiveDon(u32),
    /// N of the opponent's rested DON!! stay rested in their next Refresh Phase
    OppDonNoRefresh(u32),
    /// the count later "for each" / "for every" actions use
    SetCount(Num),
    ExtraTurn,
    WinGame,
    /// trash as many cards from your hand as the last action took
    TrashCount,
    /// K.O. up to N of the opponent's Characters whose powers total at most `budget`
    KoTotalPower { n: usize, budget: i32 },
    NoDraw,
    NoHandPlay,
    /// "you cannot attack a Leader during this turn"
    NoLeaderAttack,
    /// rest your Leader (if it fits) or 1 active DON!!: the player picks
    RestLeaderOrDon(Pred),
    NoDonActiveByChar,
    /// the opponent's [On Play] effects are negated until the end of their next turn
    OppNoOnPlay,
    /// rest any number of your active DON!! (the count is how many)
    RestDonAny,
    /// return N of your given DON!! to your cost area rested
    ReturnGivenDon(u32),
    /// move up to N given DON!! from your cards to 1 of yours that fits
    MoveGivenDon { n: u32, to: Pred },
    /// "Select your Leader and 1 Character": it is your Leader and the one chosen
    PickLeaderAnd(Pred),
    OppRestDonAtMain(u32),
    /// your Characters K.O.'d in battle this turn: you may trash 1 card from hand instead
    BattleKoShield,
    /// at the end of this turn
    LaterTrashOne(Pred),
    LaterSetActiveAll(Pred),
    LaterDonMatch,
    LaterNoRefreshOne(Pred),
    /// "it" is the opposing card in the current battle
    ItIsBattleOpponent,
    /// "Choose up to 1 A and up to 1 B from your trash. Play 1 card and play the other card rested."
    PlayPairOneRested(Pred, Pred),
    /// up to 1 of the opponent's rested Characters or DON!! won't become active next Refresh Phase
    NoRefreshCharOrDon,
    /// give 2 of the opponent's Characters -a and -b power
    TwoDebuffs(i32, i32, Until),
    /// each of the opponent's Characters gets -N power for each DON!! given to it
    DebuffPerDon(i32, Until),
}

/// Where the cards found by a search go.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LookTo {
    Hand,
    /// played (rested or not)
    Play(bool),
    /// to the top of your Life (face-up or not)
    Life(bool),
    Trash,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LookRest {
    /// left where they are (a revealed card that wasn't played)
    Stay,
    Bottom,
    TopOrBottom,
    Trash,
}

/// Conditions.
#[derive(Clone)]
pub enum Q {
    Cmp(Num, Cmp, Num),
    Not(Box<Q>),
    And(Vec<Q>),
    Or(Vec<Q>),
    Leader(Side, Pred),
    This(Pred),
    /// a card chosen earlier in the effect ("If that card is a ...")
    It(Pred),
    YourTurn,
    OppTurn,
    /// costs paid while resolving can be paid
    Payable(Vec<Act>),
    /// this card is in a battle with an opposing Character
    BattlesChar,
    /// the attack being made targets the opponent's Leader
    AttackingLeader,
    /// one of `side`'s Characters was K.O.'d this turn
    KoThisTurn(Side),
    /// the card revealed has the cost chosen ("Choose a cost and reveal ...")
    GuessHit,
    /// the card attacking now fits
    AttackerIs(Pred),
    /// the last K.O. (State::ko_ctx): of whose card, by whose effect (None: either), in battle
    Ko { owner: Option<Side>, by_opp: Option<bool>, battle: Option<bool>, pred: Option<Pred> },
    /// the last card removed from the field by an effect (State::removed_ctx)
    Removed { owner: Side, by_opp: Option<bool>, pred: Pred },
    /// the card this one is battling fits
    BattlesWith(Pred),
    /// the last Character played fits (and came from this zone)
    Played { pred: Pred, from: Option<Zone> },
    /// the last DON!! returned were returned by your effect
    DonReturnedByMe,
    /// the attack's target is this card
    TargetIsThis,
    /// a card was trashed from your hand by an effect this turn
    HandTrashedThisTurn,
    /// you activated an Event with this base cost or more this turn
    EventCostAtLeast(i32),
    /// the last card to leave a Life area went to a hand
    LifeWentToHand,
    /// the last card removed from the field went to this zone
    RemovedTo(Zone),
    /// the last cards trashed from a hand were by an effect of a card that fits
    HandTrashedBy(Pred),
    /// the last Character played was played by an effect of a card that fits
    PlayedByEffectOf(Pred),
    /// the last card removed from the field was removed by your effect
    RemovedByMe,
    /// the last card rested by an effect was rested by an effect of a card that fits
    RestedBySrc(Pred),
    Legacy(Cond),
}

impl Q {
    pub fn exists(sel: Sel) -> Q {
        Q::Cmp(Num::Count(Box::new(sel)), Cmp::Ge, Num::K(1))
    }
}

/// A replacement effect from compiled text: "If <who> would be K.O.'d / removed from the field
/// ..., you may <cost> instead."
#[derive(Clone)]
pub struct RepData {
    /// None: this card; else your cards that fit (on the field)
    pub who: Option<Pred>,
    pub ko_only: bool,
    /// Some(true): in battle only; Some(false): by effects only
    pub battle: Option<bool>,
    pub by_opp: bool,
    pub cond: Option<Q>,
    pub cost: Vec<Act>,
    pub rest_this: bool,
    /// also when it would be K.O.'d at all
    pub or_ko: bool,
    /// return N DON!! from your field to your DON!! deck
    pub don_return: u32,
    /// rest N of your active DON!!
    pub don_rest: u32,
    /// done after the cost ("... and draw 1 card instead")
    pub after: Vec<Body>,
    /// "you may ... instead" (else it always happens)
    pub optional: bool,
    pub your_turn: bool,
    pub opp_turn: bool,
}

fn sides(side: Side, me: u8) -> Vec<u8> {
    match side {
        Side::Me => vec![me],
        Side::Opp => vec![1 - me],
        Side::Both => vec![1 - me, me],
    }
}

impl<'a> Eng<'a> {
    pub fn num(&self, n: &Num, src: Uid) -> i32 {
        let me = self.owner(src);
        let sum = |side: Side, f: &dyn Fn(u8) -> i32| sides(side, me).into_iter().map(f).sum::<i32>();
        match n {
            Num::K(k) => *k,
            Num::Life(s) => sum(*s, &|p| self.ps(p).life.len() as i32),
            Num::Hand(s) => sum(*s, &|p| self.ps(p).hand.len() as i32),
            Num::Trash(s) => sum(*s, &|p| self.ps(p).trash.len() as i32),
            Num::Deck(s) => sum(*s, &|p| self.ps(p).deck.len() as i32),
            Num::DonField(s) => sum(*s, &|p| self.don_on_field(p) as i32),
            Num::DonActive(s) => sum(*s, &|p| self.ps(p).don_active as i32),
            Num::DonRested(s) => sum(*s, &|p| self.ps(p).don_rested as i32),
            Num::DonGiven(s) => {
                sum(*s, &|p| self.ps(p).field_iter().map(|c| self.c(c).don as i32).sum::<i32>())
            }
            Num::Count(sel) => self.sel_pool(sel, src).len() as i32,
            Num::ThisPower => self.power(src),
            Num::ThisDon => self.c(src).don as i32,
            Num::DonJustReturned => self.ps(me).don_just_returned as i32,
            Num::Per(x, every, times) => self.num(x, src) / every * times,
            Num::Sub(a, b) => self.num(a, src) - self.num(b, src),
            Num::TotalCost(s) => sum(*s, &|p| self.characters(p).into_iter().map(|c| self.cost(c)).sum::<i32>()),
            Num::LeaderPower(s) => sum(*s, &|p| self.power(self.ps(p).leader)),
            Num::LeaderBase(s) => sum(*s, &|p| self.base_power(self.ps(p).leader)),
            Num::ItPower => self.last.first().map_or(0, |&u| self.power(u)),
            Num::Counted => self.count as i32,
            Num::ItDon => self.last.first().map_or(0, |&u| self.c(u).don as i32),
            Num::ItCost => self.last.first().map_or(0, |&u| self.d(u).cost),
            Num::DistinctNames(sel) => {
                let mut names: Vec<&str> = self.sel_pool(sel, src).into_iter().map(|u| self.name(u)).collect();
                names.sort();
                names.dedup();
                names.len() as i32
            }
            Num::IfQ(q, a, b) => if self.q(q, src) { *a } else { *b },
            Num::HandTrashedCount => self.s.hand_trashed_ctx.0 as i32,
            Num::AttackerPower => self.s.battle.as_ref().map_or(0, |b| self.power(b.attacker)),
            Num::MyTurns => {
                let t = self.s.turn_number;
                if self.s.first_player == me { (t + 1) / 2 } else { t / 2 }
            }
            Num::Sum(a, b) => self.num(a, src) + self.num(b, src),
        }
    }

    pub fn pred_ok(&self, p: &Pred, u: Uid, src: Uid) -> bool {
        let d = self.d(u);
        let c = self.c(u);
        if !p.cats.is_empty() && !p.cats.contains(&d.category) {
            return false;
        }
        if !p.names.is_empty() && !p.names.iter().any(|n| *n == d.name) {
            return false;
        }
        if p.not_names.iter().any(|n| *n == d.name) {
            return false;
        }
        if !p.types.is_empty() && !p.types.iter().any(|t| d.has_type(t)) {
            return false;
        }
        if p.not_types.iter().any(|t| d.has_type(t)) {
            return false;
        }
        if !p.type_inc.is_empty() && !p.type_inc.iter().any(|t| d.type_includes(t)) {
            return false;
        }
        if !p.colors.is_empty() && !p.colors.iter().any(|x| d.colors.contains(x)) {
            return false;
        }
        if let Some(m) = p.multicolor {
            if (d.colors.len() > 1) != m {
                return false;
            }
        }
        if !p.attrs.is_empty() && !p.attrs.iter().any(|a| d.attributes.contains(a) || (a == "Slash" && self.has_keyword(u, Kw::AttrSlash))) {
            return false;
        }
        for (cmp, n, base) in &p.cost {
            let v = if *base { d.cost } else { self.cost(u) };
            if !cmp.ok(v, self.num(n, src)) {
                return false;
            }
        }
        for (cmp, n, base) in &p.power {
            let v = if *base { d.power } else if matches!(c.zone, Some(Zone::Character) | Some(Zone::Leader)) { self.power(u) } else { d.power };
            if !cmp.ok(v, self.num(n, src)) {
                return false;
            }
        }
        if let Some((cmp, n)) = p.counter {
            if !cmp.ok(d.counter, n) {
                return false;
            }
        }
        if let Some(r) = p.rested {
            if c.rested != r {
                return false;
            }
        }
        if let Some(t) = p.trigger {
            if d.trigger_text.is_empty() == t {
                return false;
            }
        }
        if let Some(v) = p.vanilla {
            if d.text.trim().is_empty() != v {
                return false;
            }
        }
        if p.don_min.map_or(false, |n| c.don < n) {
            return false;
        }
        if p.not_type_inc.iter().any(|t| d.type_includes(t)) {
            return false;
        }
        if p.name_inc.as_ref().map_or(false, |n| !d.name.contains(n.as_str())) {
            return false;
        }
        if p.face_up.map_or(false, |f| c.face_up != f) {
            return false;
        }
        if p.without_timing.map_or(false, |t| d.first_effect(t).is_some()) {
            return false;
        }
        if p.not_attrs.iter().any(|a| d.attributes.contains(a)) {
            return false;
        }
        if p.opp_only && c.owner == self.owner(src) {
            return false;
        }
        if p.not_color_of_it && self.last.iter().any(|&x| self.d(x).colors.iter().any(|col| d.colors.contains(col))) {
            return false;
        }
        if p.same_name_as_it && !self.last.iter().any(|&x| self.d(x).named(&d.name)) {
            return false;
        }
        if let Some(h) = p.has_don {
            if (c.don > 0) != h {
                return false;
            }
        }
        if !p.any_of.is_empty() && !p.any_of.iter().any(|x| self.pred_ok(x, u, src)) {
            return false;
        }
        !(p.not_this && u == src)
    }

    /// Every card a selection could take, in the order they are offered.
    pub fn sel_pool(&self, sel: &Sel, src: Uid) -> Vec<Uid> {
        let me = self.owner(src);
        match sel {
            Sel::This => vec![src],
            Sel::It => self.last.clone(),
            Sel::Rest => self.last.iter().copied().filter(|&u| matches!(self.c(u).zone, Some(Zone::Deck) | Some(Zone::Life))).collect(),
            Sel::Cards { side, place, pred, .. } => {
                let mut out = vec![];
                for p in sides(*side, me) {
                    let ps = self.ps(p);
                    let pool: Vec<Uid> = match place {
                        Place::Field => {
                            let cats = if pred.cats.is_empty() {
                                vec![Category::Leader, Category::Character, Category::Stage]
                            } else {
                                pred.cats.clone()
                            };
                            let mut v = vec![];
                            if cats.contains(&Category::Leader) {
                                v.push(ps.leader);
                            }
                            if cats.contains(&Category::Character) {
                                v.extend_from_slice(&ps.characters);
                            }
                            if cats.contains(&Category::Stage) {
                                v.extend(ps.stage);
                            }
                            v
                        }
                        Place::Hand => ps.hand.to_vec(),
                        Place::Trash => ps.trash.iter().rev().copied().collect(),
                        Place::Life => ps.life.iter().rev().copied().collect(),
                        Place::LifeTop(n) => ps.life[ps.life.len().saturating_sub(*n)..].iter().rev().copied().collect(),
                        Place::LifeEnds => {
                            let mut v: Vec<Uid> = ps.life.last().copied().into_iter().collect();
                            if ps.life.len() > 1 {
                                v.push(ps.life[0]);
                            }
                            v
                        }
                        Place::Deck => ps.deck.iter().rev().copied().collect(),
                        Place::HandTrash => ps.hand.iter().chain(ps.trash.iter().rev()).copied().collect(),
                        Place::DeckTop(n) => ps.deck[ps.deck.len().saturating_sub(*n)..].iter().rev().copied().collect(),
                    };
                    out.extend(pool.into_iter().filter(|&u| self.pred_ok(pred, u, src)));
                }
                out
            }
        }
    }

    pub fn q(&self, q: &Q, src: Uid) -> bool {
        let me = self.owner(src);
        match q {
            Q::Cmp(a, c, b) => c.ok(self.num(a, src), self.num(b, src)),
            Q::Not(x) => !self.q(x, src),
            Q::And(v) => v.iter().all(|x| self.q(x, src)),
            Q::Or(v) => v.iter().any(|x| self.q(x, src)),
            Q::Leader(side, p) => sides(*side, me).into_iter().all(|pl| self.pred_ok(p, self.ps(pl).leader, src)),
            Q::This(p) => self.pred_ok(p, src, src),
            Q::It(p) => !self.last.is_empty() && self.last.iter().all(|&u| self.pred_ok(p, u, src)),
            Q::YourTurn => self.s.turn_player == me,
            Q::OppTurn => self.s.turn_player != me,
            Q::Payable(a) => self.acts_payable(a, src),
            Q::BattlesChar => self.s.battle.as_ref().map_or(false, |b| {
                (b.attacker == src && self.d(b.target).category == Category::Character)
                    || (b.target == src && self.d(b.attacker).category == Category::Character)
            }),
            Q::GuessHit => self.guess.map_or(false, |k| self.last.first().map_or(false, |&u| self.d(u).cost == k)),
            Q::AttackerIs(p) => self.s.battle.as_ref().map_or(false, |b| self.pred_ok(p, b.attacker, src)),
            Q::KoThisTurn(s) => sides(*s, me).into_iter().any(|p| self.ps(p).ko_turn == self.s.turn_number),
            Q::AttackingLeader => self.s.battle.as_ref().map_or(false, |b| self.d(b.target).category == Category::Leader),
            Q::Removed { owner, by_opp, pred } => self.s.removed_ctx.map_or(false, |(c, o, by)| {
                (match owner {
                    Side::Me => o == me,
                    Side::Opp => o != me,
                    Side::Both => true,
                }) && by_opp.map_or(true, |x| (by == Some(1 - o)) == x)
                    && self.pred_ok(pred, c, src)
            }),
            Q::Played { pred, from } => self.s.played_ctx.map_or(false, |(c, z)| {
                from.map_or(true, |f| z == Some(f)) && self.pred_ok(pred, c, src)
            }),
            Q::DonReturnedByMe => self.s.don_return_by == Some(me),
            Q::HandTrashedThisTurn => self.ps(me).hand_trashed_turn == self.s.turn_number,
            Q::EventCostAtLeast(n) => {
                let (t, c) = self.ps(me).event_cost;
                t == self.s.turn_number && c >= *n
            }
            Q::RemovedByMe => self.s.removed_ctx.map_or(false, |(_, _, by)| by == Some(me)),
            Q::RestedBySrc(p) => self.s.rested_by.map_or(false, |s| self.pred_ok(p, s, src)),
            Q::LifeWentToHand => self.s.life_left_to == Some(Zone::Hand),
            Q::RemovedTo(z) => self.s.removed_ctx.map_or(false, |(c, _, _)| self.c(c).zone == Some(*z)),
            Q::HandTrashedBy(p) => self.s.hand_trashed_ctx.1.map_or(false, |s| self.pred_ok(p, s, src)),
            Q::PlayedByEffectOf(p) => self.s.played_by.map_or(false, |s| self.pred_ok(p, s, src)),
            Q::TargetIsThis => self.s.battle.as_ref().map_or(false, |b| b.target == src),
            Q::BattlesWith(p) => self.s.battle.as_ref().map_or(false, |b| {
                let other = if b.attacker == src { Some(b.target) } else if b.target == src { Some(b.attacker) } else { None };
                other.map_or(false, |o| self.pred_ok(p, o, src))
            }),
            Q::Ko { owner, by_opp, battle, pred } => self.s.ko_ctx.as_ref().map_or(false, |&(o, by, inb, card)| {
                if pred.as_ref().map_or(false, |p| !self.pred_ok(p, card, src)) {
                    return false;
                }
                owner.map_or(true, |s| match s {
                    Side::Me => o == me,
                    Side::Opp => o != me,
                    Side::Both => true,
                }) && by_opp.map_or(true, |x| (by == Some(1 - o)) == x)
                    && battle.map_or(true, |x| inb == x)
            }),
            Q::Legacy(c) => self.eval(c, src),
        }
    }

    /// Can these actions be carried out in full (costs)?
    pub fn acts_payable(&self, acts: &[Act], src: Uid) -> bool {
        acts.iter().all(|a| match (&a.sel, a.verb) {
            (Sel::This, Verb::Rest) => !self.c(src).rested,
            (Sel::This, Verb::SetActive) => self.c(src).rested,
            (Sel::This, _) => matches!(self.c(src).zone, Some(Zone::Character) | Some(Zone::Stage) | Some(Zone::Leader)),
            (Sel::It | Sel::Rest, _) => !self.last.is_empty(),
            (Sel::Cards { many, .. }, verb) => {
                let n = match many {
                    Many::Exactly(n) | Many::UpTo(n) => *n,
                    Many::All => 1, // "your active Leader": it has to be there
                };
                if Self::is_your_cards(a) {
                    return self.active_card_count(self.owner(src)) as usize >= n;
                }
                if let Verb::GiveDon { n: k, rested } = verb {
                    let ps = self.ps(self.owner(src));
                    if (if rested { ps.don_rested } else { ps.don_active }) < k {
                        return false;
                    }
                }
                let pool = self.sel_pool(&a.sel, src);
                let usable = pool
                    .iter()
                    .filter(|&&u| match verb {
                        Verb::Rest => !self.c(u).rested,
                        Verb::SetActive => self.c(u).rested,
                        Verb::Face(up) => self.c(u).face_up != up,
                        _ => true,
                    })
                    .count();
                usable >= n
            }
        })
    }

    fn verb_label(&self, v: Verb) -> String {
        match v {
            Verb::Ko => "K.O.".into(),
            Verb::Rest => "Rest".into(),
            Verb::SetActive => "Set as active".into(),
            Verb::ToHand => "Return to hand".into(),
            Verb::ToDeck { bottom, .. } => format!("Place at the {} of the deck", if bottom { "bottom" } else { "top" }),
            Verb::Trash => "Trash".into(),
            Verb::ToLife { face_up, .. } => format!("Add to Life{}", if face_up { " face-up" } else { "" }),
            Verb::Power(v, _) => format!("{v:+} power"),
            Verb::Cost(v, _) => format!("{v:+} cost"),
            Verb::BasePower(v, _) => format!("Base power becomes {v}"),
            Verb::Grant(k, _) => format!("Gains [{}]", k.as_str()),
            Verb::Negate(_) => "Negate the effect".into(),
            Verb::Play { .. } => "Play a card".into(),
            Verb::GiveDon { n, .. } => format!("Give {n} DON!!"),
            Verb::NoRefresh => "Won't become active next Refresh Phase".into(),
            Verb::Pick => "Choose".into(),
            Verb::ToHandOrDeck => "Return to hand or the bottom of the deck".into(),
            Verb::KoOrToHand => "K.O. or return to hand".into(),
            Verb::KoOrRest => "K.O. or rest".into(),
            Verb::PlayOrLife => "Play it or add it to Life".into(),
            Verb::Later(_) => "Choose".into(),
            Verb::Face(up) => format!("Turn face-{}", if up { "up" } else { "down" }),
            Verb::PowerPer(v, _) => format!("{v:+} power for each"),
            Verb::PowerSet(v, _) => format!("Power becomes {v}"),
            Verb::CostSet(v, _) => format!("Cost becomes {v}"),
            Verb::Redirect => "New target of the attack".into(),
            Verb::AtBattleEnd(_) => "Choose".into(),
            Verb::GiveOppDon { n, .. } => format!("Give {n} of your opponent's DON!!"),
            Verb::Reveal => "Reveal".into(),
        }
    }

    /// Rest a card by an effect, unless it can't be rested.
    pub fn rest_by_effect(&mut self, u: Uid, by: u8) -> R<()> {
        if self.has_keyword(u, Kw::NoRest) || self.c(u).rested {
            return Ok(());
        }
        let owner = self.owner(u);
        if by != owner && self.has_keyword(u, Kw::NoRestByOpp) {
            return Ok(());
        }
        let src_is_card = self.cur_src.map_or(false, |s| matches!(self.d(s).category, Category::Leader | Category::Character));
        if by != owner && src_is_card && self.has_keyword(u, Kw::NoRestByOppCards) {
            return Ok(()); // "cannot be rested by your opponent's Leader and Character effects"
        }
        let src_is_char = self.cur_src.map_or(false, |s| self.d(s).category == Category::Character);
        if by != owner && src_is_char && self.d(u).st.rest_swap && !self.negated(u) && self.s.turn_player != owner {
            // "If this Character would be rested by your opponent's Character's effect, you may rest 1 of your other Characters instead."
            let others: Vec<Uid> = self.characters(owner).into_iter().filter(|&c| c != u && !self.c(c).rested).collect();
            if !others.is_empty() && self.confirm(owner, &format!("{}: rest another Character instead?", self.name(u)))? {
                let c = self.choose_cards(owner, others, 1, "Rest instead", true)?[0];
                self.cm(c).rested = true;
                return Ok(());
            }
        }
        self.s.rested_by = self.cur_src;
        self.cm(u).rested = true;
        if matches!(self.c(u).zone, Some(Zone::Character) | Some(Zone::Leader) | Some(Zone::Stage)) {
            self.fire(Timing::BecameRested, &[u]);
            let field = self.ps(by).field();
            self.fire(Timing::RestedByEffect, &field); // "If a Character is rested by your effect"
            if by != self.owner(u) {
                self.fire(Timing::RestedByOpp, &[u]);
            }
        }
        Ok(())
    }

    /// "Rest N of your cards": Leader, Characters, Stage or DON!!.
    fn is_your_cards(a: &Act) -> bool {
        matches!((&a.sel, a.verb), (Sel::Cards { side: Side::Me, place: Place::Field, pred, .. }, Verb::Rest)
            if pred.cats.is_empty() && pred.names.is_empty() && pred.types.is_empty())
    }

    /// Choose the cards for an action and carry it out. Afterwards `last` holds them.
    pub fn act(&mut self, a: &Act, src: Uid) -> R<()> {
        let me = self.owner(src);
        if Self::is_your_cards(a) {
            if let Sel::Cards { many: Many::Exactly(n) | Many::UpTo(n), .. } = a.sel {
                self.rest_your_cards(me, n)?;
                self.did = true;
                return Ok(());
            }
        }
        let chooser = if a.opp_chooses { 1 - me } else { me };
        let mut pool = self.sel_pool(&a.sel, src);
        match a.verb {
            Verb::Rest => pool.retain(|&u| !self.c(u).rested && !self.has_keyword(u, Kw::NoRest)),
            Verb::SetActive => pool.retain(|&u| self.c(u).rested),
            Verb::Face(up) => pool.retain(|&u| self.c(u).face_up != up),
            Verb::Play { .. } | Verb::PlayOrLife => pool.retain(|&u| {
                matches!(self.d(u).category, Category::Character | Category::Stage) && !self.d(u).st.no_effect_play
            }),
            Verb::GiveDon { rested, .. } => {
                let have = if rested { self.ps(me).don_rested } else { self.ps(me).don_active };
                if have == 0 {
                    pool.clear();
                }
            }
            _ => {}
        }
        if let Verb::GiveOppDon { rested, .. } = a.verb {
            let o = 1 - me;
            let have = if rested { self.ps(o).don_rested } else { self.ps(o).don_rested + self.ps(o).don_active };
            if have == 0 {
                pool.clear();
            }
        }
        let chosen = match &a.sel {
            Sel::This | Sel::It | Sel::Rest => pool,
            Sel::Cards { side, place, many, .. } => match many {
                Many::All => pool,
                Many::UpTo(n) | Many::Exactly(n) => {
                    let exact = matches!(many, Many::Exactly(_));
                    let hidden = *place == Place::Hand && (*side == Side::Opp) != a.opp_chooses;
                    if hidden {
                        // a hand you can't see: the pick is effectively random (8-4-4-2)
                        let k = (*n).min(pool.len());
                        self.s.rng.sample(&pool, k)
                    } else if matches!(&a.sel, Sel::Cards { pred, .. } if pred.distinct_names) {
                        // "with different card names": one at a time, without repeating a name
                        let label = self.verb_label(a.verb);
                        let (mut left, mut got) = (pool, vec![]);
                        while got.len() < *n {
                            let pick = self.choose_cards(chooser, left.clone(), 1, &label, exact)?;
                            let Some(&c) = pick.first() else { break };
                            got.push(c);
                            let name = self.name(c);
                            left.retain(|&x| self.name(x) != name);
                        }
                        self.last = got.clone();
                        got
                    } else {
                        let label = self.verb_label(a.verb);
                        self.choose_cards(chooser, pool, *n, &label, exact)?
                    }
                }
            },
        };
        self.last = chosen.clone();
        self.did = !chosen.is_empty();
        if !matches!(a.verb, Verb::PowerPer(..)) {
            self.count = chosen.len();
        }
        if let Verb::ToDeck { bottom, ends } = a.verb {
            let bottom = if ends && !chosen.is_empty() {
                let opts = vec![
                    Action::new(ActKind::Select).index(0).label("Top of the deck"),
                    Action::new(ActKind::Select).index(1).label("Bottom of the deck"),
                ];
                self.choose(me, DecisionKind::Select, opts, "Place them at the top or bottom of the deck?")?.index == Some(1)
            } else {
                bottom
            };
            // several cards to a deck at once: the owner orders them (3-1-7)
            let (field, other): (Vec<Uid>, Vec<Uid>) =
                chosen.iter().partition(|&&u| matches!(self.c(u).zone, Some(Zone::Character) | Some(Zone::Stage)));
            for u in field {
                self.remove_from_field(u, Zone::Deck, Some(me), bottom, false)?;
            }
            for p in [me, 1 - me] {
                let mine: Vec<Uid> = other.iter().copied().filter(|&u| self.owner(u) == p).collect();
                if !mine.is_empty() {
                    self.place_in_order(p, mine, Zone::Deck, bottom)?;
                }
            }
            return Ok(());
        }
        for u in chosen {
            self.apply(a.verb, u, me)?;
        }
        Ok(())
    }

    fn apply(&mut self, v: Verb, u: Uid, me: u8) -> R<()> {
        let on_field = matches!(self.c(u).zone, Some(Zone::Character) | Some(Zone::Stage));
        match v {
            Verb::Ko => {
                if self.c(u).zone == Some(Zone::Character) {
                    self.ko(u, Some(me), true, false)?;
                } else if self.c(u).zone == Some(Zone::Stage) {
                    self.remove_from_field(u, Zone::Trash, Some(me), false, false)?;
                }
            }
            Verb::Rest => self.rest_by_effect(u, me)?,
            Verb::SetActive => self.cm(u).rested = false,
            Verb::ToHand => {
                if on_field {
                    self.remove_from_field(u, Zone::Hand, Some(me), false, false)?;
                } else {
                    if self.c(u).zone == Some(Zone::Deck) || self.c(u).zone == Some(Zone::Life) {
                        let (o, n) = (self.owner(u), self.name(u));
                        if self.c(u).zone == Some(Zone::Deck) {
                            self.log(|| format!("Player {o} reveals {n} and adds it to their hand"));
                        }
                    }
                    let from_deck = self.c(u).zone == Some(Zone::Deck);
                    self.mv(u, Zone::Hand);
                    if from_deck {
                        self.cm(u).revealed = true;
                    }
                }
            }
            Verb::ToDeck { bottom, .. } => {
                if on_field {
                    self.remove_from_field(u, Zone::Deck, Some(me), bottom, false)?;
                } else {
                    self.move_card(u, Some(Zone::Deck), bottom, false);
                }
            }
            Verb::Trash => {
                if on_field {
                    self.remove_from_field(u, Zone::Trash, Some(me), false, false)?;
                } else {
                    let from_hand = self.c(u).zone == Some(Zone::Hand);
                    self.mv(u, Zone::Trash);
                    if from_hand {
                        let o = self.owner(u);
                        self.hand_trashed(o, 1);
                    }
                }
            }
            Verb::ToLife { face_up, ends, bottom } => {
                let owner = self.owner(u);
                let bottom = if ends {
                    self.confirm(owner, &format!("Put {} at the bottom of the Life cards?", self.name(u)))?
                } else {
                    bottom
                };
                if on_field {
                    self.remove_from_field(u, Zone::Life, Some(me), bottom, face_up)?;
                } else {
                    self.move_card(u, Some(Zone::Life), bottom, face_up);
                }
            }
            Verb::Power(x, until) => self.add_power(u, x, until, Some(me)),
            Verb::Cost(x, until) => self.add_cost(u, x, until, Some(me)),
            Verb::BasePower(x, until) => self.set_base_power(u, x, until, Some(me)),
            Verb::Grant(k, until) => self.grant_keyword(u, k, until, Some(me)),
            Verb::Negate(until) => self.negate(u, until, Some(me)),
            Verb::Play { rested } => {
                if matches!(self.d(u).category, Category::Character | Category::Stage) {
                    self.play_card(u, rested)?;
                }
            }
            Verb::GiveDon { n, rested } => {
                self.give_don(me, u, n, rested);
            }
            Verb::NoRefresh => self.skip_next_refresh(u, me),
            Verb::Pick => {}
            Verb::ToHandOrDeck | Verb::KoOrToHand => {
                let name = self.name(u);
                let first = if v == Verb::KoOrToHand { format!("K.O. {name}") } else { format!("Return {name} to hand") };
                let opts = vec![
                    Action::new(ActKind::Select).index(0).label(self.lbl(|| first)),
                    Action::new(ActKind::Select).index(1).label(self.lbl(|| {
                        if v == Verb::KoOrToHand { format!("Return {name} to hand") } else { format!("Place {name} at the bottom of the deck") }
                    })),
                ];
                let second = self.choose(me, DecisionKind::Select, opts, "Which?")?.index == Some(1);
                match (v, second) {
                    (Verb::KoOrToHand, false) => self.ko(u, Some(me), true, false)?,
                    (Verb::KoOrToHand, true) | (Verb::ToHandOrDeck, false) => {
                        self.remove_from_field(u, Zone::Hand, Some(me), false, false)?;
                    }
                    _ => {
                        self.remove_from_field(u, Zone::Deck, Some(me), true, false)?;
                    }
                }
            }
            Verb::PlayOrLife => {
                let name = self.name(u);
                let mut opts = vec![Action::new(ActKind::Select).index(1).label(self.lbl(|| format!("Add {name} to the top of your Life face-up")))];
                if matches!(self.d(u).category, Category::Character | Category::Stage) {
                    opts.insert(0, Action::new(ActKind::Select).index(0).label(self.lbl(|| format!("Play {name}"))));
                }
                if self.choose(me, DecisionKind::Select, opts, "Play it or add it to your Life?")?.index == Some(0) {
                    self.play_card(u, false)?;
                } else {
                    self.move_card(u, Some(Zone::Life), false, true);
                }
            }
            Verb::KoOrRest => {
                let name = self.name(u);
                let opts = vec![
                    Action::new(ActKind::Select).index(0).label(self.lbl(|| format!("K.O. {name}"))),
                    Action::new(ActKind::Select).index(1).label(self.lbl(|| format!("Rest {name}"))),
                ];
                if self.choose(me, DecisionKind::Select, opts, "K.O. or rest it?")?.index == Some(0) {
                    self.ko(u, Some(me), true, false)?;
                } else {
                    self.rest_by_effect(u, me)?;
                }
            }
            Verb::Later(lv) => {
                let (turn, inc) = (self.s.turn_number, self.c(u).incarnation);
                self.s.delayed.push(Delayed { turn, player: me, kind: DelayedKind::Card(u, inc, lv) });
            }
            Verb::Face(up) => self.cm(u).face_up = up,
            Verb::PowerPer(v, until) => {
                let k = self.count as i32;
                if k > 0 {
                    self.add_power(u, v * k, until, Some(me));
                }
            }
            Verb::PowerSet(v, until) => self.add_mod_pub(u, ModKind::PowerSet(v), until, Some(me)),
            Verb::CostSet(v, until) => self.add_mod_pub(u, ModKind::CostSet(v), until, Some(me)),
            Verb::AtBattleEnd(lv) => {
                let inc = self.c(u).incarnation;
                self.s.battle_end.push((u, inc, lv, me));
            }
            Verb::Redirect => {
                let inc = self.c(u).incarnation;
                if let Some(b) = self.s.battle.as_mut() {
                    b.target = u;
                    b.target_inc = inc;
                }
                let n = self.name(u);
                self.log(|| format!("The attack's target becomes {n}"));
            }
            Verb::GiveOppDon { n, rested } => {
                let o = self.owner(u);
                for _ in 0..n {
                    let ps = self.ps(o);
                    let from_rested = if rested || ps.don_active == 0 {
                        true
                    } else if ps.don_rested == 0 {
                        false
                    } else {
                        let opts = vec![
                            Action::new(ActKind::Select).index(0).label("A rested DON!!"),
                            Action::new(ActKind::Select).index(1).label("An active DON!!"),
                        ];
                        self.choose(me, DecisionKind::Select, opts, "Which DON!! to give?")?.index == Some(0)
                    };
                    self.give_don(o, u, 1, from_rested);
                }
            }
            Verb::Reveal => {
                let (o, n) = (self.owner(u), self.name(u));
                self.log(|| format!("Player {o} reveals {n}"));
                if self.c(u).zone == Some(Zone::Hand) {
                    self.cm(u).revealed = true;
                }
            }
        }
        Ok(())
    }

    pub fn do_(&mut self, d: &Do, src: Uid) -> R<()> {
        let me = self.owner(src);
        self.did = true;
        match d {
            Do::Draw(n) => self.effect_draw(me, *n, me),
            Do::OppDraw(n) => self.effect_draw(1 - me, *n, me),
            Do::ReturnDon { opp, n } => {
                let p = if *opp { 1 - me } else { me };
                self.s.don_return_by = Some(me);
                self.return_don(p, *n)?;
            }
            Do::RestDon { opp, n } => {
                let p = if *opp { 1 - me } else { me };
                let ps = self.psm(p);
                let k = (*n).min(ps.don_active);
                ps.don_active -= k;
                ps.don_rested += k;
            }
            Do::Shuffle => {
                let mut deck = std::mem::take(&mut self.psm(me).deck);
                self.s.rng.shuffle(&mut deck);
                self.psm(me).deck = deck;
            }
            Do::RevealTop => {
                let top = self.ps(me).deck.last().copied();
                self.last = top.into_iter().collect();
                if let Some(t) = top {
                    let n = self.name(t);
                    self.log(|| format!("Player {me} reveals {n} from the top of their deck"));
                }
            }
            Do::TrashOppLife(n) => {
                for _ in 0..*n {
                    let Some(&top) = self.ps(1 - me).life.last() else { break };
                    self.mv(top, Zone::Trash);
                }
            }
            Do::DeckToLife(n) => {
                for _ in 0..*n {
                    let Some(&top) = self.ps(me).deck.last() else { break };
                    self.mv(top, Zone::Life);
                }
            }
            Do::DonActiveAtEnd(n) => self.psm(me).don_active_at_end += *n,
            Do::Damage(n) => self.deal_damage(1 - me, *n, false)?,
            Do::TrashLifeUntil(n) => {
                while self.ps(me).life.len() > *n {
                    let top = *self.ps(me).life.last().unwrap();
                    self.mv(top, Zone::Trash);
                }
            }
            Do::ReturnDonAtLeast(min) => {
                let max = self.don_on_field(me);
                let opts = (*min..=max).map(|k| Action::new(ActKind::Select).index(k as i32).label(format!("Return {k} DON!!"))).collect();
                let k = self.choose(me, DecisionKind::Select, opts, "How many DON!! to return?")?.index.unwrap() as u32;
                self.return_don(me, k)?;
            }
            Do::PeekLife => {
                let opts: Vec<Action> = [me, 1 - me]
                    .into_iter()
                    .filter(|&p| !self.ps(p).life.is_empty())
                    .map(|p| Action::new(ActKind::Select).index(p as i32).label(if p == me { "Your Life" } else { "Your opponent's Life" }))
                    .collect();
                if opts.is_empty() {
                    return Ok(());
                }
                let mut opts = opts;
                opts.insert(0, Action::new(ActKind::Pass).label("Don't look"));
                let a = self.choose(me, DecisionKind::Select, opts, "Look at the top card of which Life?")?;
                let Some(p) = a.index else { return Ok(()) };
                let top = *self.ps(p as u8).life.last().unwrap();
                let face_up = self.c(top).face_up;
                let opts = vec![
                    Action::new(ActKind::Select).index(0).label(self.lbl(|| format!("Keep {} on top", self.name(top)))),
                    Action::new(ActKind::Select).index(1).label(self.lbl(|| format!("Put {} at the bottom", self.name(top)))),
                ];
                if self.choose(me, DecisionKind::Select, opts, "Top or bottom of the Life cards?")?.index == Some(1) {
                    self.move_card(top, Some(Zone::Life), true, face_up);
                }
            }
            Do::DrawUpTo(n) => {
                let n = (*n).min(self.ps(me).deck.len() as u32);
                let opts = (0..=n).map(|k| Action::new(ActKind::Select).index(k as i32).label(format!("Draw {k}"))).collect();
                let k = self.choose(me, DecisionKind::Select, opts, "How many cards to draw?")?.index.unwrap();
                self.effect_draw(me, k as usize, me);
            }
            Do::DrawTo(n) => {
                let have = self.ps(me).hand.len();
                if have < *n {
                    self.effect_draw(me, n - have, me);
                }
            }
            Do::DrawCount(x) => {
                let k = self.num(x, src).max(0) as usize;
                self.count = k;
                self.effect_draw(me, k, me);
            }
            Do::TrashTo { n, both } => {
                let ps: Vec<u8> = if *both { vec![self.s.turn_player, 1 - self.s.turn_player] } else { vec![me] };
                for p in ps {
                    let extra = self.ps(p).hand.len().saturating_sub(*n);
                    if extra > 0 {
                        self.trash_from_hand(p, extra)?;
                    }
                }
            }
            Do::TakeDamage(n) => self.deal_damage(me, *n, false)?,
            Do::ReorderTop(n) => {
                let deck = &self.ps(me).deck;
                let top: Vec<Uid> = deck[deck.len().saturating_sub(*n)..].iter().rev().copied().collect();
                self.place_in_order(me, top, Zone::Deck, false)?;
            }
            Do::ReorderLife { opp } => {
                let p = if *opp { 1 - me } else { me };
                let life: Vec<Uid> = self.ps(p).life.iter().rev().copied().collect();
                let faces: Vec<(Uid, bool)> = life.iter().map(|&c| (c, self.c(c).face_up)).collect();
                // the effect's player orders them; the owner's Life keeps each card's face
                let mut left = life.clone();
                let mut order = vec![];
                while left.len() > 1 {
                    let opts = left.iter().map(|&c| Action::new(ActKind::Select).card(c).label(self.lbl(|| self.name(c).to_string()))).collect();
                    let a = self.choose(me, DecisionKind::OrderCards, opts, "Order the Life cards: pick the next one from the top")?;
                    let c = a.card.unwrap();
                    order.push(c);
                    left.retain(|&x| x != c);
                }
                order.extend(left);
                for &c in order.iter().rev() {
                    let up = faces.iter().find(|(x, _)| *x == c).unwrap().1;
                    self.move_card(c, Some(Zone::Life), false, up);
                }
            }
            Do::DeckToLifeLater(n) => {
                let t = self.s.turn_number;
                self.s.delayed.push(Delayed { turn: t, player: me, kind: DelayedKind::DeckToLife(*n) });
            }
            Do::AddDonLater(n) => {
                let t = self.s.turn_number;
                self.s.delayed.push(Delayed { turn: t, player: me, kind: DelayedKind::AddDonActive(*n) });
            }
            Do::RestDonOrChar { n, pred } => {
                let o = 1 - me;
                for _ in 0..*n {
                    let mut opts = vec![Action::new(ActKind::Pass).label("Done")];
                    if self.ps(o).don_active > 0 {
                        opts.push(Action::new(ActKind::Select).index(0).label("Rest 1 active DON!!"));
                    }
                    for c in self.characters(o) {
                        if !self.c(c).rested && !self.has_keyword(c, Kw::NoRest) && self.pred_ok(pred, c, src) {
                            opts.push(Action::new(ActKind::Select).card(c).label(self.lbl(|| format!("Rest {}", self.describe(c)))));
                        }
                    }
                    if opts.len() == 1 {
                        break;
                    }
                    let a = self.choose(me, DecisionKind::Select, opts, "Rest your opponent's DON!! or a Character")?;
                    match (a.kind, a.card) {
                        (ActKind::Pass, _) => break,
                        (_, Some(c)) => self.rest_by_effect(c, me)?,
                        _ => {
                            let ps = self.psm(o);
                            ps.don_active -= 1;
                            ps.don_rested += 1;
                        }
                    }
                }
            }
            Do::GuessCost => {
                let o = 1 - me;
                let opts = (0..=10).map(|k| Action::new(ActKind::Select).index(k).label(format!("Cost {k}"))).collect();
                let k = self.choose(me, DecisionKind::Select, opts, "Choose a cost")?.index.unwrap();
                self.guess = Some(k);
                let top = self.ps(o).deck.last().copied();
                self.last = top.into_iter().collect();
                if let Some(t) = top {
                    let (n, c) = (self.name(t), self.d(t).cost);
                    self.log(|| format!("Player {me} chooses cost {k} and reveals {n} (cost {c})"));
                }
            }
            Do::LookOppTop => {
                self.log(|| format!("Player {me} looks at the top card of their opponent's deck"));
            }
            Do::SwapBase(until) => {
                if let [a, b] = self.last[..] {
                    let (pa, pb) = (self.base_power(a), self.base_power(b));
                    self.set_base_power(a, pb, *until, Some(me));
                    self.set_base_power(b, pa, *until, Some(me));
                }
            }
            Do::BasePowerFrom(n, until) => {
                let v = self.num(n, src);
                self.set_base_power(src, v, *until, Some(me));
            }
            Do::ActivateEvent { trash, pred } => {
                let ps = self.ps(me);
                let pool: Vec<Uid> = if *trash { ps.trash.iter().rev().copied().collect() } else { ps.hand.to_vec() };
                let cands = pool
                    .into_iter()
                    .filter(|&c| {
                        self.d(c).category == Category::Event
                            && self.pred_ok(pred, c, src)
                            && self.d(c).first_effect(Timing::Main).map_or(false, |i| self.can_activate(c, i))
                    })
                    .collect();
                for c in self.choose_cards(me, cands, 1, "Activate an Event", false)? {
                    let i = self.d(c).first_effect(Timing::Main).unwrap();
                    let n = self.name(c);
                    self.log(|| format!("Player {me} activates {n}"));
                    if !*trash {
                        self.mv(c, Zone::Trash);
                    }
                    self.activate(c, i)?;
                }
            }
            Do::NextPlayDiscount(pred, n) => {
                let t = self.s.turn_number;
                self.psm(me).next_play_discount = Some((t, pred.clone(), *n));
            }
            Do::GiveDonToOwner { any_area } => {
                let mut opts = vec![Action::new(ActKind::Pass).label("Done")];
                for p in [me, 1 - me] {
                    let ps = self.ps(p);
                    if ps.don_rested + if *any_area { ps.don_active } else { 0 } == 0 {
                        continue;
                    }
                    for c in self.leader_and_characters(p) {
                        opts.push(Action::new(ActKind::Select).card(c).label(self.lbl(|| format!("Give DON!! to {}", self.name(c)))));
                    }
                }
                if opts.len() > 1 {
                    let a = self.choose(me, DecisionKind::Select, opts, "Give 1 DON!! to its owner's Leader or Character")?;
                    if let Some(c) = a.card {
                        let o = self.owner(c);
                        let rested = self.ps(o).don_rested > 0;
                        self.give_don(o, c, 1, rested);
                    }
                }
            }
            Do::ShuffleOpp => {
                let o = 1 - me;
                let mut deck = std::mem::take(&mut self.psm(o).deck);
                self.s.rng.shuffle(&mut deck);
                self.psm(o).deck = deck;
            }
            Do::LifeToDeckTop(n) => {
                let life: Vec<Uid> = self.ps(me).life.iter().rev().copied().collect();
                for c in self.choose_cards(me, life, *n, "Place on top of your deck", true)? {
                    self.mv(c, Zone::Deck);
                }
                self.do_(&Do::ReorderLife { opp: false }, src)?;
            }
            Do::ExtraTurn => self.psm(me).extra_turn = true,
            Do::WinGame => {
                self.log(|| format!("Player {me} wins the game by an effect"));
                return Err(crate::engine::Stop::Over(Some(me)));
            }
            Do::TrashCount => {
                let k = self.count.min(self.ps(me).hand.len());
                self.trash_from_hand(me, k)?;
            }
            Do::KoTotalPower { n, budget } => {
                let (mut left, mut chosen) = (*budget, vec![]);
                while chosen.len() < *n {
                    let cands = self.characters(1 - me).into_iter().filter(|c| !chosen.contains(c) && self.power(*c) <= left).collect();
                    let pick = self.choose_cards(me, cands, 1, &format!("K.O. Characters with a total power of {budget} or less"), false)?;
                    let Some(&p) = pick.first() else { break };
                    chosen.push(p);
                    left -= self.power(p);
                }
                for c in chosen {
                    self.ko(c, Some(me), true, false)?;
                }
            }
            Do::NoDraw => self.psm(me).no_draw_turn = self.s.turn_number,
            Do::NoHandPlay => self.psm(me).no_hand_play_turn = self.s.turn_number,
            Do::RestLeaderOrDon(pred) => {
                let l = self.ps(me).leader;
                let mut opts = vec![];
                if self.pred_ok(pred, l, src) {
                    opts.push(Action::new(ActKind::Select).card(l).label("Rest your Leader"));
                }
                if self.ps(me).don_active > 0 {
                    opts.push(Action::new(ActKind::Select).index(0).label("Rest 1 of your DON!! cards"));
                }
                if opts.is_empty() {
                    return Ok(());
                }
                let a = self.choose(me, DecisionKind::Cost, opts, "Pay the cost")?;
                if a.card.is_some() {
                    self.cm(l).rested = true;
                } else {
                    let ps = self.psm(me);
                    ps.don_active -= 1;
                    ps.don_rested += 1;
                }
            }
            Do::NoLeaderAttack => self.psm(me).no_leader_attack_turn = self.s.turn_number,
            Do::NoDonActiveByChar => self.psm(me).no_don_active_by_char_turn = self.s.turn_number,
            Do::OppNoOnPlay => {
                let until = self.s.turn_number + if self.s.turn_player == me { 1 } else { 0 };
                self.psm(1 - me).on_play_off_until = until;
            }
            Do::RestDonAny => {
                let max = self.ps(me).don_active;
                let opts = (0..=max).map(|k| Action::new(ActKind::Select).index(k as i32).label(format!("Rest {k} DON!!"))).collect();
                let k = self.choose(me, DecisionKind::Select, opts, "How many DON!! to rest?")?.index.unwrap() as u32;
                let ps = self.psm(me);
                ps.don_active -= k;
                ps.don_rested += k;
                self.count = k as usize;
            }
            Do::ReturnGivenDon(n) => {
                for _ in 0..*n {
                    let cands: Vec<Uid> = self.ps(me).field().into_iter().filter(|&c| self.c(c).don > 0).collect();
                    let Some(&c) = self.choose_cards(me, cands, 1, "Return 1 given DON!!", true)?.first() else { break };
                    self.cm(c).don -= 1;
                    self.psm(me).don_rested += 1;
                }
            }
            Do::MoveGivenDon { n, to } => {
                let mut moved = 0;
                while moved < *n {
                    let from: Vec<Uid> = self.ps(me).field().into_iter().filter(|&c| self.c(c).don > 0).collect();
                    let Some(&f) = self.choose_cards(me, from, 1, "Take a given DON!! from", false)?.first() else { break };
                    let dests = self.ps(me).field().into_iter().filter(|&c| c != f && self.pred_ok(to, c, src)).collect();
                    let Some(&t) = self.choose_cards(me, dests, 1, "Give it to", true)?.first() else { break };
                    self.cm(f).don -= 1;
                    self.cm(t).don += 1;
                    moved += 1;
                }
            }
            Do::PickLeaderAnd(pred) => {
                let leader = self.ps(me).leader;
                let cands = self.characters(me).into_iter().filter(|&c| self.pred_ok(pred, c, src)).collect();
                let mut got = vec![leader];
                got.extend(self.choose_cards(me, cands, 1, "Select 1 Character", true)?);
                self.last = got;
            }
            Do::OppRestDonAtMain(n) => self.psm(1 - me).rest_don_at_main += *n,
            Do::BattleKoShield => self.psm(me).battle_ko_shield_turn = self.s.turn_number,
            Do::LaterTrashOne(p) | Do::LaterSetActiveAll(p) | Do::LaterNoRefreshOne(p) => {
                let kind = match d {
                    Do::LaterTrashOne(_) => DelayedKind::TrashOne(p.clone()),
                    Do::LaterSetActiveAll(_) => DelayedKind::SetActiveAll(p.clone()),
                    _ => DelayedKind::NoRefreshOne(p.clone()),
                };
                let t = self.s.turn_number;
                self.s.delayed.push(Delayed { turn: t, player: me, kind });
            }
            Do::LaterDonMatch => {
                let t = self.s.turn_number;
                self.s.delayed.push(Delayed { turn: t, player: me, kind: DelayedKind::DonMatch });
            }
            Do::PlayPairOneRested(a, b) => {
                let pool = |e: &Eng, p: &Pred| -> Vec<Uid> {
                    e.ps(me).trash.iter().rev().copied().filter(|&c| e.d(c).category == Category::Character && !e.d(c).st.no_effect_play && e.pred_ok(p, c, src)).collect()
                };
                let first = self.choose_cards(me, pool(self, a), 1, "Choose a Character card", false)?;
                let second: Vec<Uid> = pool(self, b).into_iter().filter(|c| !first.contains(c)).collect();
                let second = self.choose_cards(me, second, 1, "Choose a Character card", false)?;
                let mut both: Vec<Uid> = first.into_iter().chain(second).collect();
                if both.len() == 2 {
                    let active = self.choose_cards(me, both.clone(), 1, "Play this one active (the other rested)", true)?[0];
                    both.retain(|&c| c != active);
                    self.play_card(active, false)?;
                    self.play_card(both[0], true)?;
                } else if let Some(&c) = both.first() {
                    self.play_card(c, false)?;
                }
            }
            Do::NoRefreshCharOrDon => {
                let o = 1 - me;
                let mut opts = vec![Action::new(ActKind::Pass).label("Done")];
                if self.ps(o).don_rested > 0 {
                    opts.push(Action::new(ActKind::Select).index(0).label("1 rested DON!!"));
                }
                for c in self.characters(o).into_iter().filter(|&c| self.c(c).rested) {
                    opts.push(Action::new(ActKind::Select).card(c).label(self.lbl(|| self.describe(c))));
                }
                if opts.len() > 1 {
                    let a = self.choose(me, DecisionKind::Select, opts, "Won't become active next Refresh Phase")?;
                    match (a.kind, a.card) {
                        (ActKind::Pass, _) => {}
                        (_, Some(c)) => self.skip_next_refresh(c, me),
                        _ => self.psm(o).don_skip_refresh += 1,
                    }
                }
            }
            Do::TwoDebuffs(a, b, until) => {
                let mut cands = self.characters(1 - me);
                for v in [*a, *b] {
                    let Some(&c) = self.choose_cards(me, cands.clone(), 1, &format!("-{v} power"), false)?.first() else { break };
                    self.add_power(c, -v, *until, Some(me));
                    cands.retain(|&x| x != c);
                }
            }
            Do::DebuffPerDon(v, until) => {
                for c in self.characters(1 - me) {
                    let k = self.c(c).don as i32;
                    if k > 0 {
                        self.add_power(c, -v * k, *until, Some(me));
                    }
                }
            }
            Do::ItIsBattleOpponent => {
                self.last = self
                    .s
                    .battle
                    .as_ref()
                    .map(|b| if b.attacker == src { b.target } else { b.attacker })
                    .into_iter()
                    .collect();
            }
            Do::SetCount(n) => {
                self.count = self.num(n, src).max(0) as usize;
            }
            Do::MillCount => {
                for _ in 0..self.count {
                    let Some(&top) = self.ps(me).deck.last() else { break };
                    self.mv(top, Zone::Trash);
                }
            }
            Do::OppReturnActiveDon(n) => {
                self.s.don_return_by = Some(me);
                let ps = self.psm(1 - me);
                let k = (*n).min(ps.don_active);
                ps.don_active -= k;
                ps.don_deck += k;
                ps.don_just_returned = k;
                let field = self.ps(1 - me).field();
                self.fire(Timing::DonReturned, &field);
            }
            Do::OppDonNoRefresh(n) => {
                let ps = self.psm(1 - me);
                ps.don_skip_refresh = (ps.don_skip_refresh + *n).min(ps.don_rested);
            }
            Do::ItIsAttacker => {
                self.last = self.s.battle.as_ref().map(|b| b.attacker).into_iter().collect();
            }
            Do::OppAddDon(n) => {
                self.add_don(1 - me, *n, false);
            }
            Do::SetAllDonActive => {
                let ps = self.psm(me);
                ps.don_active += ps.don_rested;
                ps.don_rested = 0;
            }
            Do::NoPlayCost(c) => {
                let t = self.s.turn_number;
                self.psm(me).no_play_cost = (t, *c);
            }
            Do::TurnLife { up } => {
                for c in self.ps(me).life.to_vec() {
                    self.cm(c).face_up = *up;
                }
            }
            Do::Look { n, k, pred, rest, to } => {
                let play = &matches!(to, LookTo::Play(_));
                let deck = &self.ps(me).deck;
                let top: Vec<Uid> = deck[deck.len().saturating_sub(*n)..].iter().rev().copied().collect();
                if *play {
                    let names: Vec<&str> = top.iter().map(|&c| self.name(c)).collect();
                    self.log(|| format!("Player {me} reveals {}", names.join(", ")));
                }
                let cands = top
                    .iter()
                    .copied()
                    .filter(|&c| self.pred_ok(pred, c, src))
                    .filter(|&c| !*play || (matches!(self.d(c).category, Category::Character | Category::Stage) && !self.d(c).st.no_effect_play))
                    .collect();
                let label = match to {
                    LookTo::Hand => "Add to hand",
                    LookTo::Play(_) => "Play a card",
                    LookTo::Life(_) => "Add to the top of your Life",
                    LookTo::Trash => "Trash",
                };
                let picks = self.choose_cards(me, cands, *k, label, false)?;
                match to {
                    LookTo::Play(rested) => {
                        for &c in &picks {
                            self.play_card(c, *rested)?;
                        }
                    }
                    LookTo::Life(up) => {
                        for &c in &picks {
                            self.move_card(c, Some(Zone::Life), false, *up);
                        }
                    }
                    LookTo::Trash => {
                        for &c in &picks {
                            self.mv(c, Zone::Trash);
                        }
                    }
                    LookTo::Hand => {}
                }
                let picks = if *to == LookTo::Hand { picks } else { vec![] };
                if !picks.is_empty() {
                    let names: Vec<&str> = picks.iter().map(|&c| self.name(c)).collect();
                    let which = if picks.len() == 1 { "it" } else { "them" };
                    self.log(|| format!("Player {me} reveals {} and adds {which} to their hand", names.join(", ")));
                }
                for &c in &picks {
                    self.mv(c, Zone::Hand);
                    self.cm(c).revealed = true;
                }
                let left: Vec<Uid> = top.into_iter().filter(|&c| !picks.contains(&c) && self.c(c).zone == Some(Zone::Deck)).collect();
                match rest {
                    LookRest::Stay => {}
                    LookRest::Trash => {
                        for c in left {
                            self.mv(c, Zone::Trash);
                        }
                    }
                    LookRest::Bottom => self.place_in_order(me, left, Zone::Deck, true)?,
                    LookRest::TopOrBottom => {
                        if !left.is_empty() {
                            let opts = vec![
                                Action::new(ActKind::Select).index(0).label("Top of your deck"),
                                Action::new(ActKind::Select).index(1).label("Bottom of your deck"),
                            ];
                            let bottom = self.choose(me, DecisionKind::Select, opts, "Place the rest at the top or bottom of your deck?")?.index == Some(1);
                            self.place_in_order(me, left, Zone::Deck, bottom)?;
                        }
                    }
                }
                self.last = picks;
            }
            Do::NoLifeToHand => {
                let t = self.s.turn_number;
                self.psm(me).no_life_to_hand_turn = t;
            }
        }
        Ok(())
    }

    /// A compiled replacement: does it apply to `card` leaving the field?
    pub fn rep_applies(&self, r: &RepData, src: Uid, card: Uid, by: Option<u8>, ko: bool, in_battle: bool) -> bool {
        let me = self.owner(src);
        let main = !(r.ko_only && !ko) && r.battle.map_or(true, |b| b == in_battle) && !(r.by_opp && by != Some(1 - me));
        if !(main || (r.or_ko && ko)) {
            return false;
        }
        if r.your_turn && self.s.turn_player != me {
            return false;
        }
        if r.opp_turn && self.s.turn_player == me {
            return false;
        }
        let who_ok = match &r.who {
            None => card == src,
            Some(p) => self.owner(card) == me && self.pred_ok(p, card, src),
        };
        who_ok && r.cond.as_ref().map_or(true, |q| self.q(q, src)) && (!r.rest_this || !self.c(src).rested) && self.don_on_field(me) >= r.don_return && self.ps(me).don_active >= r.don_rest && self.acts_payable(&r.cost, src)
    }

    /// Pay a compiled replacement for `card` ("it" in its text).
    pub fn rep_pay(&mut self, r: &RepData, src: Uid, card: Uid) -> R<()> {
        self.last = vec![card];
        if r.rest_this {
            self.cm(src).rested = true;
        }
        if r.don_return > 0 {
            self.return_don(self.owner(src), r.don_return)?;
        }
        if r.don_rest > 0 {
            let ps = self.psm(self.owner(src));
            ps.don_active -= r.don_rest;
            ps.don_rested += r.don_rest;
        }
        for a in &r.cost {
            self.act(a, src)?;
        }
        for b in &r.after {
            self.run(b, src)?;
        }
        Ok(())
    }
}

/// A readable form of a compiled body, for checking what the compiler read.
pub fn show(b: &Body) -> String {
    match b {
        Body::Seq(a, c) => format!("{}; {}", show(a), show(c)),
        Body::If(c, x) => format!("IF[{}] {{{}}}", show_cond(c), show(x)),
        Body::UnlessOppTrashes(n, x) => format!("UNLESS opp trashes {n} {{{}}}", show(x)),
        Body::Optional(x) => format!("MAY {{{}}}", show(x)),
        Body::ChooseOne { opponent, options } => format!(
            "CHOOSE{} [{}]",
            if *opponent { "(opp)" } else { "" },
            options.iter().map(|(_, b)| show(b)).collect::<Vec<_>>().join(" | ")
        ),
        Body::WithCosts { body, .. } => format!("COSTS(legacy) {{{}}}", show(body)),
        Body::T(_) => "TEMPLATE".into(),
        Body::Script(_) => "SCRIPT".into(),
        Body::Act(a) => format!("{:?}{} {}", a.verb, if a.opp_chooses { "(opp)" } else { "" }, show_sel(&a.sel)),
        Body::Do(d) => format!("{d:?}"),
        Body::Pay { costs, ask, body } => format!(
            "PAY{}[{}] {{{}}}",
            if *ask { "?" } else { "" },
            costs.iter().map(|a| format!("{:?} {}", a.verb, show_sel(&a.sel))).collect::<Vec<_>>().join(", "),
            show(body)
        ),
        Body::IfDid(a, c) => format!("{{{}}} IF DID {{{}}}", show(a), show(c)),
        Body::OppMay(x) => format!("OPP MAY {{{}}}", show(x)),
        Body::OppMayElse(x, y) => format!("OPP MAY {{{}}} ELSE {{{}}}", show(x), show(y)),
        Body::Nothing => "nothing".into(),
    }
}

pub fn show_sel(s: &Sel) -> String {
    match s {
        Sel::This => "this".into(),
        Sel::It => "it".into(),
        Sel::Rest => "the rest".into(),
        Sel::Cards { side, place, many, pred } => format!("{many:?} {side:?} {place:?} {}", show_pred(pred)),
    }
}

pub fn show_pred(p: &Pred) -> String {
    let d = format!("{p:?}");
    // drop the empty fields
    let re = regex::Regex::new(r"\w+: (?:\[\]|None|false), ?").unwrap();
    re.replace_all(&d, "").replace("Pred { ", "{").replace(" }", "}")
}

pub fn show_cond(c: &Cond) -> String {
    match c {
        Cond::Q(q) => show_q(q),
        Cond::All(v) if v.is_empty() => "true".into(),
        _ => "legacy".into(),
    }
}

pub fn show_q(q: &Q) -> String {
    match q {
        Q::Cmp(a, c, b) => format!("{} {c:?} {}", show_num(a), show_num(b)),
        Q::Not(x) => format!("NOT {}", show_q(x)),
        Q::And(v) => v.iter().map(show_q).collect::<Vec<_>>().join(" AND "),
        Q::Or(v) => v.iter().map(show_q).collect::<Vec<_>>().join(" OR "),
        Q::Leader(s, p) => format!("{s:?} Leader {}", show_pred(p)),
        Q::This(p) => format!("this {}", show_pred(p)),
        Q::It(p) => format!("it {}", show_pred(p)),
        Q::YourTurn => "your turn".into(),
        Q::OppTurn => "opp turn".into(),
        Q::Payable(_) => "payable".into(),
        Q::BattlesChar => "battles a Character".into(),
        Q::AttackingLeader => "attacking the Leader".into(),
        Q::KoThisTurn(s) => format!("{s:?} Character K.O.'d this turn"),
        Q::GuessHit => "revealed card has the chosen cost".into(),
        Q::AttackerIs(p) => format!("attacker {}", show_pred(p)),
        Q::Ko { owner, by_opp, battle, .. } => format!("K.O. owner={owner:?} by_opp={by_opp:?} battle={battle:?}"),
        Q::Removed { owner, by_opp, pred } => format!("removed {owner:?} by_opp={by_opp:?} {}", show_pred(pred)),
        Q::BattlesWith(p) => format!("battles {}", show_pred(p)),
        Q::Played { pred, from } => format!("played {} from {from:?}", show_pred(pred)),
        Q::DonReturnedByMe => "DON!! returned by your effect".into(),
        Q::TargetIsThis => "this is the attack's target".into(),
        Q::Legacy(_) => "legacy".into(),
        q => format!("{}", match q { Q::HandTrashedThisTurn => "hand trashed this turn", Q::EventCostAtLeast(_) => "event cost", Q::LifeWentToHand => "life went to hand", Q::RemovedTo(_) => "removed to", Q::HandTrashedBy(_) => "hand trashed by", Q::PlayedByEffectOf(_) => "played by effect of", _ => "?" }),
    }
}

fn show_num(n: &Num) -> String {
    match n {
        Num::Count(s) => format!("count({})", show_sel(s)),
        Num::Sum(a, b) => format!("{}+{}", show_num(a), show_num(b)),
        x => format!("{x:?}"),
    }
}

impl std::fmt::Debug for Q {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", show_q(self))
    }
}
