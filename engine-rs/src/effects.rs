//! Effects as data, and the interpreter that carries them out. The compiler (compile.rs) turns
//! card text into these; scripts.rs adds hand-written functions. Each template here matches a
//! `t_*` function in optcg/effect_text.py and does the same things in the same order.

use crate::cards::*;
use crate::engine::{Eng, R};

pub type BodyFn = for<'a, 'b> fn(&'b mut Eng<'a>, Uid) -> R<()>;
pub type CondFn = for<'a, 'b> fn(&'b Eng<'a>, Uid) -> bool;
pub type CardNumFn = for<'a, 'b> fn(&'b Eng<'a>, Uid, Uid) -> i32;
pub type CardOptFn = for<'a, 'b> fn(&'b Eng<'a>, Uid, Uid) -> Option<i32>;
pub type HandOptFn = for<'a, 'b> fn(&'b Eng<'a>, Uid) -> Option<i32>;
pub type KoFilterFn = for<'a, 'b> fn(&'b Eng<'a>, Uid, Uid) -> bool;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dir {
    Eq,
    Less,
    More,
}

impl Dir {
    pub fn ok(self, value: i32, n: i32) -> bool {
        match self {
            Dir::Eq => value == n,
            Dir::Less => value <= n,
            Dir::More => value >= n,
        }
    }
}

/// effect_text.COND_PARTS, plus the conditions effects and statics carry.
#[derive(Clone)]
pub enum Cond {
    All(Vec<Cond>),
    LeaderIsOrHasType(String, String),
    LeaderHasType(String),
    LeaderTypeIncludes(String),
    LeaderIs(String),
    OppLeaderPowerAtLeast(i32),
    OppLife(i32, Dir),
    MyLife(i32, Dir),
    MyHand(i32, Dir),
    DonOnFieldAtLeast(u32),
    AnyCharCostAtLeast(i32),
    MyCharCostAtLeast(i32),
    OppCharPowerAtLeast(i32),
    NoOther(String),
    CharsWithBasePower(usize, i32),
    CharsWithTrigger(usize),
    PlayedThisTurn,
    FaceUpLife,
    OppHand(i32, Dir),
    MyTrashAtLeast(usize),
    NoCharCostAtLeast(i32),
    LeaderHasAttr(String),
    OppCharBasePowerAtLeast(i32),
    OppCharCostIs(i32),
    /// the number of DON!! on your field is at least N less than on your opponent's
    DonBehindBy(i32),
    /// The [DON!! xX] / [Your Turn] / [Opponent's Turn] checks of a permanent effect.
    Spec { don_req: u32, your_turn: bool, opp_turn: bool, extra: Option<Box<Cond>> },
    /// Costs paid while resolving can be paid (effect_text._wrap_costs's `payable`).
    Payable(Box<Costs>),
    Script(CondFn),
}

/// Card filter phrases (effect_text.parse_filter). Reads printed values.
#[derive(Clone, Debug)]
pub enum Filter {
    Names { names: Vec<String>, cost: Option<(i32, Dir)> },
    General {
        color: Option<String>,
        types: Vec<String>,
        cat: Option<Category>,
        inc: Option<String>,
        trig: bool,
        cost: Option<(i32, Dir)>,
        /// (value, "or less"); without "or less" the power must be exactly the value
        power: Option<(i32, bool)>,
        attr: Option<String>,
        excl: Option<String>,
    },
    /// "[Megalo] or {Neptunian} type cards": either, and not named `excl`
    Either { left: Box<Filter>, right: Box<Filter>, excl: Option<String> },
}

/// "N of your opponent's (rested) Characters with a (base) cost of X (or less) / X (base) power or less".
#[derive(Clone, Debug)]
pub struct OppPred {
    pub rested: bool,
    pub cost: Option<(i32, bool, bool)>, // (value, base cost, or less)
    pub power: Option<(i32, bool)>,      // (value, base power)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pool {
    Leader,
    Characters,
    LeaderAndCharacters,
}

/// One part of "your X or up to N of your Y" (effect_text._own_candidates).
#[derive(Clone, Debug)]
pub struct OwnPart {
    pub pool: Pool,
    pub types: Vec<String>,
    pub name: Option<String>,
    pub orname: Option<String>,
    pub inc: Option<String>,
    /// "with both the {X} type and a [Trigger]"
    pub both: Option<String>,
    /// "with the <A> (or <B>) attribute"
    pub attrs: Vec<String>,
    /// current power at most
    pub power_max: Option<i32>,
    /// current cost at least
    pub cost_min: Option<i32>,
}

/// Costs paid while resolving (effect_text._wrap_costs).
#[derive(Clone, Debug, Default)]
pub struct Costs {
    pub trash: Option<(usize, Filter)>,
    pub reveal: Option<(usize, Filter)>,
    pub life: usize,
    pub rest_cards: usize,
    pub life_face_up: usize,
    pub trash_self: bool,
    pub life_face_down: usize,
    pub give_leader_don: u32,
    pub return_active_don: u32,
    /// rest your Leader, which must have this name
    pub rest_leader: Option<String>,
    pub ko_own: Option<(usize, Vec<OwnPart>)>,
    pub trash_own: Option<(usize, Vec<OwnPart>)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Where {
    Hand,
    Deck,
}

/// effect_text.TEMPLATES, one variant per `t_*` builder.
#[derive(Clone)]
pub enum Tpl {
    Ko { n: usize, pred: OppPred },
    KoTotal { budget: i32 },
    BottomAll { pred: OppPred },
    RestLeaderOrChar { n: usize },
    NoCharacterPlay,
    SetActive { n: usize, who: Vec<OwnPart> },
    SelfGain { kw: Option<Kw>, v: i32, until: Until },
    Rest { n: usize, pred: OppPred },
    RestAll,
    CannotAttack { n: usize, pred: OppPred },
    KoStage { n: usize },
    Debuff { n: usize, v: i32, cost: bool, with_leader: bool, rested: bool, min_power: Option<i32>, until: Until },
    GiveAny { n: usize, v: i32, label: String },
    OppLeaderDebuff { v: i32 },
    SkipRefresh { n: usize, pred: OppPred },
    Negate { n: usize, pred: OppPred, back: bool },
    KoOrRest { n: usize, pred: OppPred },
    RestByLife { n: usize },
    KoOwn { n: usize, who: Vec<OwnPart> },
    BuffAll { bp: i32, v: i32, until: Until },
    ProtectAll { bp: i32, t: String },
    NoBlockerAll { cost: i32 },
    GiveRestedDonTo { n: u32, who: Vec<OwnPart> },
    DebuffAll { v: i32, cost: bool },
    Bounce { n: usize, opp_only: bool, to: Where, cost: Option<i32>, trig: bool },
    DrawTrash { d: usize, t: usize },
    Draw { d: usize },
    TrashHand { t: usize },
    OppTrashes { n: usize },
    TrashOppHand { n: usize },
    OppBottoms { n: usize },
    Mill { n: usize },
    LifeToHand { n: usize, opp: bool },
    DeckToLife { n: usize },
    AddDon { n: u32, rested: bool },
    SetDonActive { n: u32 },
    GiveRestedDon { n: u32, chars: bool, name: Option<String> },
    Buff { n: usize, v: i32, until: Until, who: Vec<OwnPart> },
    Grant { n: usize, kw: Kw, who: Vec<OwnPart> },
    LeaderBuff { v: i32, until: Until, types: Vec<String> },
    LeaderKeyword { kw: Kw, name: Option<String> },
    LeaderBasePower { v: i32, who: String, until: Until },
    Unblockable,
    PlaySelf,
    ActivateOwn { timing: Timing },
    Look { n: usize, k: usize, pred: Filter, to_trash: bool },
    Scry { n: usize },
    PlayFrom { n: usize, hand: bool, trash: bool, hand_first: bool, rested: bool, pred: Filter },
    CharToLife { n: usize, opp_only: bool, cost: i32 },
    HandToLife { n: usize, pred: Filter, face_up: bool },
    TrashToHand { n: usize, pred: Filter },
}

#[derive(Clone)]
pub enum Body {
    Seq(Box<Body>, Box<Body>),
    If(Cond, Box<Body>),
    UnlessOppTrashes(usize, Box<Body>),
    Optional(Box<Body>),
    ChooseOne { opponent: bool, options: Vec<(String, Body)> },
    WithCosts { costs: Box<Costs>, ask: bool, body: Box<Body> },
    T(Tpl),
    Script(BodyFn),
}

#[derive(Clone)]
pub enum Target {
    SelfCard,
    AllYourCharacters { types: Option<String>, min_cost: Option<i32> },
}

#[derive(Clone)]
pub enum StaticNum {
    Parts(Vec<(Cond, i32, Target)>),
    Script(CardNumFn),
}

/// A replacement effect (8-1-3-4) that stops a card leaving the field.
#[derive(Clone)]
pub struct Replacement {
    pub applies: for<'a, 'b> fn(&'b Eng<'a>, Uid, Uid, Option<u8>, bool) -> bool,
    pub pay: for<'a, 'b> fn(&'b mut Eng<'a>, Uid, Uid) -> R<()>,
    pub payable: CondFn,
    pub text: String,
    pub once_per_turn: bool,
}

/// "If you would take damage, ... instead" (applies to `player`'s damage).
#[derive(Clone)]
pub struct DamageReplacement {
    pub applies: for<'a, 'b> fn(&'b Eng<'a>, Uid, u8) -> bool,
    pub pay: BodyFn,
    pub payable: CondFn,
    pub text: String,
}

/// Permanent effects of a card (the static_* / hand_* / attack_lock fields of CardDef in Python).
#[derive(Clone, Default)]
pub struct Statics {
    pub power: Option<StaticNum>,
    pub cost: Option<StaticNum>,
    pub keywords: Vec<(Cond, Kw, Target)>,
    pub base_power: Option<CardOptFn>,
    pub counter: Option<CardOptFn>,
    pub hand_cost: Vec<(Cond, i32)>,
    pub hand_counter: Option<HandOptFn>,
    pub attack_lock: Option<CondFn>,
    pub replacements: Vec<Replacement>,
    pub damage_replacements: Vec<DamageReplacement>,
}

fn until_of(dur: &str) -> Until {
    match dur {
        "battle" => Until::Battle,
        "turn" => Until::Turn,
        _ => Until::NextEnd,
    }
}

pub fn until_from(dur: Option<&str>) -> Until {
    until_of(dur.unwrap_or(""))
}

impl<'a> Eng<'a> {
    fn leader_of(&self, src: Uid) -> Uid {
        self.ps(self.owner(src)).leader
    }

    // ------------------------------------------------------------ conditions

    pub fn eval(&self, c: &Cond, src: Uid) -> bool {
        let me = self.owner(src);
        let leader = || self.leader_of(src);
        match c {
            Cond::All(v) => v.iter().all(|x| self.eval(x, src)),
            Cond::LeaderIsOrHasType(n, t) => self.name(leader()) == n || self.d(leader()).has_type(t),
            Cond::LeaderHasType(t) => self.d(leader()).has_type(t),
            Cond::LeaderTypeIncludes(t) => self.d(leader()).type_includes(t),
            Cond::LeaderIs(n) => self.name(leader()) == n,
            Cond::OppLeaderPowerAtLeast(n) => self.power(self.ps(1 - me).leader) >= *n,
            Cond::OppLife(n, d) => d.ok(self.ps(1 - me).life.len() as i32, *n),
            Cond::MyLife(n, d) => d.ok(self.ps(me).life.len() as i32, *n),
            Cond::MyHand(n, d) => d.ok(self.ps(me).hand.len() as i32, *n),
            Cond::DonOnFieldAtLeast(n) => self.don_on_field(me) >= *n,
            Cond::AnyCharCostAtLeast(n) => {
                (0..2u8).any(|p| self.characters(p).into_iter().any(|c| self.cost(c) >= *n))
            }
            Cond::MyCharCostAtLeast(n) => self.characters(me).into_iter().any(|c| self.cost(c) >= *n),
            Cond::OppCharPowerAtLeast(n) => self.characters(1 - me).into_iter().any(|c| self.power(c) >= *n),
            Cond::NoOther(n) => !self.characters(me).into_iter().any(|c| c != src && self.name(c) == n),
            Cond::CharsWithBasePower(k, p) => {
                self.characters(me).into_iter().filter(|&c| self.base_power(c) >= *p).count() >= *k
            }
            Cond::CharsWithTrigger(k) => {
                self.characters(me).into_iter().filter(|&c| !self.d(c).trigger_text.is_empty()).count() >= *k
            }
            Cond::PlayedThisTurn => self.c(src).played_turn == self.s.turn_number,
            Cond::FaceUpLife => self.ps(me).life.iter().any(|&c| self.c(c).face_up),
            Cond::OppHand(n, d) => d.ok(self.ps(1 - me).hand.len() as i32, *n),
            Cond::MyTrashAtLeast(n) => self.ps(me).trash.len() >= *n,
            Cond::NoCharCostAtLeast(n) => !self.characters(me).into_iter().any(|c| self.cost(c) >= *n),
            Cond::LeaderHasAttr(a) => self.d(leader()).attributes.contains(a),
            Cond::OppCharBasePowerAtLeast(n) => self.characters(1 - me).into_iter().any(|c| self.base_power(c) >= *n),
            Cond::OppCharCostIs(n) => self.characters(1 - me).into_iter().any(|c| self.cost(c) == *n),
            Cond::DonBehindBy(n) => self.don_on_field(1 - me) as i32 - self.don_on_field(me) as i32 >= *n,
            Cond::Spec { don_req, your_turn, opp_turn, extra } => {
                if *don_req > 0 && self.c(src).don < *don_req {
                    return false;
                }
                if *your_turn && self.s.turn_player != me {
                    return false;
                }
                if *opp_turn && self.s.turn_player == me {
                    return false;
                }
                extra.as_ref().map_or(true, |x| self.eval(x, src))
            }
            Cond::Payable(costs) => self.payable(costs, src),
            Cond::Script(f) => f(self, src),
        }
    }

    pub fn filter_ok(&self, f: &Filter, u: Uid) -> bool {
        let d = self.d(u);
        match f {
            Filter::Names { names, cost } => {
                names.iter().any(|n| *n == d.name) && cost.map_or(true, |(n, dir)| dir.ok(d.cost, n))
            }
            Filter::Either { left, right, excl } => {
                (self.filter_ok(left, u) || self.filter_ok(right, u)) && excl.as_ref().map_or(true, |x| d.name != *x)
            }
            Filter::General { color, types, cat, inc, trig, cost, power, attr, excl } => {
                if let Some(col) = color {
                    if !d.colors.contains(col) {
                        return false;
                    }
                }
                if !types.is_empty() && !types.iter().any(|t| d.has_type(t)) {
                    return false;
                }
                if let Some(i) = inc {
                    if !d.type_includes(i) {
                        return false;
                    }
                }
                if *trig && d.trigger_text.is_empty() {
                    return false;
                }
                if let Some(c) = cat {
                    if d.category != *c {
                        return false;
                    }
                }
                if let Some((n, dir)) = cost {
                    if !dir.ok(d.cost, *n) {
                        return false;
                    }
                }
                if let Some((p, or_less)) = power {
                    if if *or_less { d.power > *p } else { d.power != *p } {
                        return false;
                    }
                }
                if let Some(a) = attr {
                    if !d.attributes.contains(a) {
                        return false;
                    }
                }
                if let Some(x) = excl {
                    if d.name == *x {
                        return false;
                    }
                }
                true
            }
        }
    }

    fn opp_ok(&self, p: &OppPred, u: Uid) -> bool {
        if p.rested && !self.c(u).rested {
            return false;
        }
        if let Some((n, base, orless)) = p.cost {
            let cost = if base { self.d(u).cost } else { self.cost(u) };
            return if orless { cost <= n } else { cost == n };
        }
        if let Some((n, base)) = p.power {
            return (if base { self.d(u).power } else { self.power(u) }) <= n;
        }
        true
    }

    fn opp_targets(&self, src: Uid, p: &OppPred) -> Vec<Uid> {
        self.characters(1 - self.owner(src)).into_iter().filter(|&c| self.opp_ok(p, c)).collect()
    }

    pub fn own_candidates(&self, parts: &[OwnPart], p: u8) -> Vec<Uid> {
        let mut out: Vec<Uid> = vec![];
        for part in parts {
            let pool = match part.pool {
                Pool::Leader => vec![self.ps(p).leader],
                Pool::Characters => self.characters(p),
                Pool::LeaderAndCharacters => self.leader_and_characters(p),
            };
            for c in pool {
                let d = self.d(c);
                if !part.types.is_empty() && !part.types.iter().any(|t| d.has_type(t)) {
                    continue;
                }
                if part.name.as_ref().map_or(false, |n| *n != d.name) {
                    continue;
                }
                if let Some(o) = &part.orname {
                    if !(d.category == Category::Character || d.name == *o) {
                        continue;
                    }
                }
                if part.inc.as_ref().map_or(false, |i| !d.type_includes(i)) {
                    continue;
                }
                if part.both.as_ref().map_or(false, |t| !(d.has_type(t) && !d.trigger_text.is_empty())) {
                    continue;
                }
                if !part.attrs.is_empty() && !part.attrs.iter().any(|a| d.attributes.contains(a)) {
                    continue;
                }
                if part.power_max.map_or(false, |p| self.power(c) > p) {
                    continue;
                }
                if part.cost_min.map_or(false, |m| self.cost(c) < m) {
                    continue;
                }
                if !out.contains(&c) {
                    out.push(c);
                }
            }
        }
        out
    }

    // ------------------------------------------------------------ permanent effects

    fn target_ok(&self, t: &Target, src: Uid, card: Uid) -> bool {
        match t {
            Target::SelfCard => card == src,
            Target::AllYourCharacters { types, min_cost } => {
                self.owner(card) == self.owner(src)
                    && self.c(card).zone == Some(Zone::Character)
                    && types.as_ref().map_or(true, |t| self.d(card).has_type(t))
                    && min_cost.map_or(true, |m| self.cost(card) >= m)
            }
        }
    }

    fn static_num(&self, s: &StaticNum, src: Uid, card: Uid) -> i32 {
        match s {
            StaticNum::Script(f) => f(self, src, card),
            StaticNum::Parts(parts) => parts
                .iter()
                .filter(|(cond, _, to)| self.target_ok(to, src, card) && self.eval(cond, src))
                .map(|(_, v, _)| *v)
                .sum(),
        }
    }

    pub fn static_power(&self, src: Uid, card: Uid) -> i32 {
        self.d(src).st.power.as_ref().map_or(0, |s| self.static_num(s, src, card))
    }

    pub fn static_cost(&self, src: Uid, card: Uid) -> i32 {
        self.d(src).st.cost.as_ref().map_or(0, |s| self.static_num(s, src, card))
    }

    pub fn static_keyword(&self, src: Uid, card: Uid, kw: Kw) -> bool {
        self.d(src)
            .st
            .keywords
            .iter()
            .any(|(cond, k, to)| *k == kw && self.target_ok(to, src, card) && self.eval(cond, src))
    }

    pub fn hand_cost(&self, card: Uid) -> i32 {
        self.d(card).st.hand_cost.iter().filter(|(cond, _)| self.eval(cond, card)).map(|(_, v)| *v).sum()
    }

    // ------------------------------------------------------------ costs paid while resolving

    fn payable(&self, k: &Costs, src: Uid) -> bool {
        let me = self.owner(src);
        let ps = self.ps(me);
        if let Some((n, f)) = &k.trash {
            if ps.hand.iter().filter(|&&c| self.filter_ok(f, c)).count() < *n {
                return false;
            }
        }
        if let Some((n, f)) = &k.reveal {
            if ps.hand.iter().filter(|&&c| self.filter_ok(f, c)).count() < *n {
                return false;
            }
        }
        if k.life > 0 && ps.life.len() < k.life {
            return false;
        }
        if k.rest_cards > 0 && (self.active_card_count(me) as usize) < k.rest_cards {
            return false;
        }
        // turning cards face-up that already are does nothing, so it can't pay the cost (1-3-2-1)
        if k.life_face_up > 0 {
            let n = k.life_face_up;
            if ps.life.len() < n || ps.life[ps.life.len() - n..].iter().any(|&c| self.c(c).face_up) {
                return false;
            }
        }
        if k.trash_self && !matches!(self.c(src).zone, Some(Zone::Character) | Some(Zone::Stage)) {
            return false;
        }
        // likewise turning face-down cards that already are (1-3-2-1)
        if k.life_face_down > 0 {
            let n = k.life_face_down;
            if ps.life.len() < n || !ps.life[ps.life.len() - n..].iter().all(|&c| self.c(c).face_up) {
                return false;
            }
        }
        let don = k.give_leader_don.max(k.return_active_don);
        if don > 0 && ps.don_active < don {
            return false;
        }
        if let Some(name) = &k.rest_leader {
            if self.name(ps.leader) != name || self.c(ps.leader).rested {
                return false;
            }
        }
        if let Some((n, who)) = &k.ko_own {
            if self.own_candidates(who, me).len() < *n {
                return false;
            }
        }
        if let Some((n, who)) = &k.trash_own {
            if self.own_candidates(who, me).len() < *n {
                return false;
            }
        }
        true
    }

    fn pay_and_run(&mut self, k: &Costs, ask: bool, body: &Body, src: Uid) -> R<()> {
        if !self.payable(k, src) {
            return Ok(());
        }
        let me = self.owner(src);
        if ask && !self.confirm(me, &format!("Pay the cost of {}'s effect?", self.name(src)))? {
            return Ok(());
        }
        if let Some((n, f)) = &k.trash {
            let cands = self.ps(me).hand.iter().copied().filter(|&c| self.filter_ok(f, c)).collect();
            for c in self.choose_cards(me, cands, *n, "Trash for the cost", true)? {
                self.mv(c, Zone::Trash);
            }
        }
        if let Some((n, f)) = &k.reveal {
            let cands = self.ps(me).hand.iter().copied().filter(|&c| self.filter_ok(f, c)).collect();
            let shown = self.choose_cards(me, cands, *n, "Reveal for the cost", true)?;
            let names: Vec<&str> = shown.iter().map(|&c| self.name(c)).collect();
            self.log(|| format!("Player {me} reveals {}", names.join(", ")));
        }
        if k.rest_cards > 0 {
            self.rest_your_cards(me, k.rest_cards)?;
        }
        if k.life_face_up > 0 {
            let life = &self.ps(me).life;
            for c in life[life.len() - k.life_face_up..].to_vec() {
                self.cm(c).face_up = true;
            }
        }
        for _ in 0..k.life {
            let top = *self.ps(me).life.last().unwrap();
            self.mv(top, Zone::Hand);
        }
        if k.trash_self {
            self.mv(src, Zone::Trash);
        }
        if k.life_face_down > 0 {
            let life = &self.ps(me).life;
            for c in life[life.len() - k.life_face_down..].to_vec() {
                self.cm(c).face_up = false;
            }
        }
        if k.give_leader_don > 0 {
            let l = self.ps(me).leader;
            self.give_don(me, l, k.give_leader_don, false);
        }
        if k.return_active_don > 0 {
            let ps = self.psm(me);
            ps.don_active -= k.return_active_don;
            ps.don_deck += k.return_active_don;
            let field = self.ps(me).field();
            self.fire(Timing::DonReturned, &field);
        }
        if k.rest_leader.is_some() {
            let l = self.ps(me).leader;
            self.cm(l).rested = true;
        }
        if let Some((n, who)) = &k.ko_own {
            // 8-3-1-7: a K.O. that is replaced or prevented doesn't pay the cost
            let cands = self.own_candidates(who, me);
            for c in self.choose_cards(me, cands, *n, "K.O. for the cost", true)? {
                self.ko(c, Some(me), true, false)?;
                if self.c(c).zone != Some(Zone::Trash) {
                    return Ok(());
                }
            }
        }
        if let Some((n, who)) = &k.trash_own {
            let cands = self.own_candidates(who, me);
            for c in self.choose_cards(me, cands, *n, "Trash for the cost", true)? {
                self.mv(c, Zone::Trash);
            }
        }
        self.run(body, src)
    }

    // ------------------------------------------------------------ bodies

    pub fn run(&mut self, b: &Body, src: Uid) -> R<()> {
        match b {
            Body::Seq(a, b2) => {
                self.run(a, src)?;
                self.run(b2, src)
            }
            Body::If(c, inner) => {
                if self.eval(c, src) {
                    self.run(inner, src)?;
                }
                Ok(())
            }
            Body::UnlessOppTrashes(n, otherwise) => {
                let o = 1 - self.owner(src);
                if self.ps(o).hand.len() >= *n
                    && self.confirm(o, &format!("Trash {n} cards to stop {}?", self.name(src)))?
                {
                    self.trash_from_hand(o, *n)?;
                    return Ok(());
                }
                self.run(otherwise, src)
            }
            Body::Optional(inner) => {
                if self.confirm(self.owner(src), &format!("Use {}'s effect?", self.name(src)))? {
                    self.run(inner, src)?;
                }
                Ok(())
            }
            Body::ChooseOne { opponent, options } => {
                let chooser = if *opponent { 1 - self.owner(src) } else { self.owner(src) };
                let opts = options
                    .iter()
                    .enumerate()
                    .map(|(i, (text, _))| Action::new(ActKind::Select).index(i as i32).label(text.clone()))
                    .collect();
                let a = self.choose(chooser, DecisionKind::ChooseOne, opts, &format!("{}: choose one", self.name(src)))?;
                self.run(&options[a.index.unwrap() as usize].1, src)
            }
            Body::WithCosts { costs, ask, body } => self.pay_and_run(costs, *ask, body, src),
            Body::T(t) => self.template(t, src),
            Body::Script(f) => f(self, src),
        }
    }

    fn template(&mut self, t: &Tpl, src: Uid) -> R<()> {
        let me = self.owner(src);
        let o = 1 - me;
        match t {
            Tpl::Ko { n, pred } => {
                let cands = self.opp_targets(src, pred);
                for c in self.choose_cards(me, cands, *n, "K.O. a Character", false)? {
                    self.ko(c, Some(me), true, false)?;
                }
            }
            Tpl::KoTotal { budget } => {
                let (mut left, mut chosen) = (*budget, vec![]);
                loop {
                    let cands = self
                        .characters(o)
                        .into_iter()
                        .filter(|c| !chosen.contains(c) && self.cost(*c) <= left)
                        .collect();
                    let pick = self.choose_cards(
                        me,
                        cands,
                        1,
                        &format!("K.O. Characters with total cost {budget} or less"),
                        false,
                    )?;
                    let Some(&p) = pick.first() else { break };
                    chosen.push(p);
                    left -= self.cost(p);
                }
                for c in chosen {
                    self.ko(c, Some(me), true, false)?;
                }
            }
            Tpl::BottomAll { pred } => {
                for c in self.opp_targets(src, pred) {
                    self.remove_from_field(c, Zone::Deck, Some(me), true, false)?;
                }
            }
            Tpl::RestLeaderOrChar { n } => {
                let cands = self.leader_and_characters(o);
                for c in self.choose_cards(me, cands, *n, "Rest a card", false)? {
                    self.cm(c).rested = true;
                }
            }
            Tpl::NoCharacterPlay => self.forbid_character_play(me),
            Tpl::SetActive { n, who } => {
                let rested = self.own_candidates(who, me).into_iter().filter(|&c| self.c(c).rested).collect();
                for c in self.choose_cards(me, rested, *n, "Set as active", false)? {
                    self.cm(c).rested = false;
                }
            }
            Tpl::SelfGain { kw, v, until } => {
                if let Some(k) = kw {
                    self.grant_keyword(src, *k, *until, Some(me));
                }
                if *v != 0 {
                    self.add_power(src, *v, *until, Some(me));
                }
            }
            Tpl::Rest { n, pred } => {
                let cands = self.opp_targets(src, pred);
                for c in self.choose_cards(me, cands, *n, "Rest a Character", false)? {
                    self.cm(c).rested = true;
                }
            }
            Tpl::RestAll => {
                for c in self.characters(o) {
                    self.cm(c).rested = true;
                }
            }
            Tpl::CannotAttack { n, pred } => {
                let cands = self.opp_targets(src, pred);
                for c in self.choose_cards(me, cands, *n, "Character can't attack", false)? {
                    self.grant_keyword(c, Kw::CannotAttack, Until::NextEnd, Some(me));
                }
            }
            Tpl::KoStage { n } => {
                let cands = self.ps(o).stage.into_iter().collect();
                for c in self.choose_cards(me, cands, *n, "K.O. a Stage", false)? {
                    self.mv(c, Zone::Trash);
                }
            }
            Tpl::Debuff { n, v, cost, with_leader, rested, min_power, until } => {
                let pool = if *with_leader { self.leader_and_characters(o) } else { self.characters(o) };
                let cands = pool
                    .into_iter()
                    .filter(|&c| (self.c(c).rested || !rested) && min_power.map_or(true, |m| self.power(c) >= m))
                    .collect();
                let what = if *cost { "cost" } else { "power" };
                for c in self.choose_cards(me, cands, *n, &format!("Give -{v} {what}"), false)? {
                    if *cost {
                        self.add_cost(c, -v, *until, Some(me));
                    } else {
                        self.add_power(c, -v, *until, Some(me));
                    }
                }
            }
            Tpl::GiveAny { n, v, label } => {
                let mut cands = self.characters(me);
                cands.extend(self.characters(o));
                for c in self.choose_cards(me, cands, *n, label, false)? {
                    self.add_power(c, *v, Until::Turn, Some(me));
                }
            }
            Tpl::OppLeaderDebuff { v } => {
                let l = self.ps(o).leader;
                self.add_power(l, -v, Until::Turn, Some(me));
            }
            Tpl::SkipRefresh { n, pred } => {
                let cands = self.opp_targets(src, pred);
                for c in self.choose_cards(me, cands, *n, "Won't become active next Refresh Phase", false)? {
                    self.skip_next_refresh(c, me);
                }
            }
            Tpl::Negate { n, pred, back } => {
                let cands = self.opp_targets(src, pred);
                for c in self.choose_cards(me, cands, *n, "Negate the effect", false)? {
                    self.negate(c, Until::Turn, Some(me));
                    if *back {
                        self.remove_from_field(c, Zone::Hand, Some(me), false, false)?;
                    }
                }
            }
            Tpl::KoOrRest { n, pred } => {
                let cands = self.opp_targets(src, pred);
                for c in self.choose_cards(me, cands, *n, "K.O. or rest a Character", false)? {
                    let name = self.name(c);
                    let opts = vec![
                        Action::new(ActKind::Select).index(0).label(self.lbl(|| format!("K.O. {name}"))),
                        Action::new(ActKind::Select).index(1).label(self.lbl(|| format!("Rest {name}"))),
                    ];
                    if self.choose(me, DecisionKind::Select, opts, "K.O. or rest it?")?.index == Some(0) {
                        self.ko(c, Some(me), true, false)?;
                    } else {
                        self.cm(c).rested = true;
                    }
                }
            }
            Tpl::RestByLife { n } => {
                let limit = (self.ps(0).life.len() + self.ps(1).life.len()) as i32;
                let cands = self.characters(o).into_iter().filter(|&c| self.cost(c) <= limit).collect();
                for c in self.choose_cards(me, cands, *n, "Rest a Character", false)? {
                    self.cm(c).rested = true;
                }
            }
            Tpl::KoOwn { n, who } => {
                let cands = self.own_candidates(who, me);
                for c in self.choose_cards(me, cands, *n, "K.O. a Character", false)? {
                    self.ko(c, Some(me), true, false)?;
                }
            }
            Tpl::BuffAll { bp, v, until } => {
                for c in self.characters(me) {
                    if self.base_power(c) <= *bp {
                        self.add_power(c, *v, *until, Some(me));
                    }
                }
            }
            Tpl::ProtectAll { bp, t } => {
                for c in self.characters(me) {
                    if self.base_power(c) == *bp && self.d(c).has_type(t) {
                        self.grant_keyword(c, Kw::NoKoInBattle, Until::NextEnd, Some(me));
                    }
                }
            }
            Tpl::NoBlockerAll { cost } => {
                for c in self.characters(o) {
                    if self.cost(c) == *cost {
                        self.grant_keyword(c, Kw::NoBlocker, Until::Turn, Some(me));
                    }
                }
            }
            Tpl::GiveRestedDonTo { n, who } => {
                if self.ps(me).don_rested == 0 {
                    return Ok(());
                }
                let cands = self.own_candidates(who, me);
                for c in self.choose_cards(me, cands, 1, "Give rested DON!!", false)? {
                    self.give_don(me, c, *n, true);
                }
            }
            Tpl::DebuffAll { v, cost } => {
                for c in self.characters(o) {
                    if *cost {
                        self.add_cost(c, -v, Until::Turn, Some(me));
                    } else {
                        self.add_power(c, -v, Until::Turn, Some(me));
                    }
                }
            }
            Tpl::Bounce { n, opp_only, to, cost, trig } => {
                let sides: Vec<u8> = if *opp_only { vec![o] } else { vec![o, me] };
                let cands = sides
                    .into_iter()
                    .flat_map(|p| self.characters(p))
                    .filter(|&c| cost.map_or(true, |x| self.cost(c) <= x) && (!trig || !self.d(c).trigger_text.is_empty()))
                    .collect();
                let zone = if *to == Where::Deck { Zone::Deck } else { Zone::Hand };
                for c in self.choose_cards(me, cands, *n, "Choose a Character to remove", false)? {
                    self.remove_from_field(c, zone, Some(me), *to == Where::Deck, false)?;
                }
            }
            Tpl::DrawTrash { d, t } => {
                self.draw(me, *d);
                self.trash_from_hand(me, *t)?;
            }
            Tpl::Draw { d } => self.draw(me, *d),
            Tpl::TrashHand { t } => {
                self.trash_from_hand(me, *t)?;
            }
            Tpl::OppTrashes { n } => {
                self.trash_from_hand(o, *n)?;
            }
            Tpl::TrashOppHand { n } => {
                // You can't see the cards, so the choice is effectively random (8-4-4-2).
                let hand = self.ps(o).hand.to_vec();
                let k = (*n).min(hand.len());
                for c in self.s.rng.sample(&hand, k) {
                    self.mv(c, Zone::Trash);
                }
            }
            Tpl::OppBottoms { n } => {
                let hand = self.ps(o).hand.to_vec();
                let chosen = self.choose_cards(o, hand, *n, "Place on the bottom of your deck", true)?;
                self.place_in_order(o, chosen, Zone::Deck, true)?;
            }
            Tpl::Mill { n } => {
                for _ in 0..(*n).min(self.ps(me).deck.len()) {
                    let top = *self.ps(me).deck.last().unwrap();
                    self.mv(top, Zone::Trash);
                }
            }
            Tpl::LifeToHand { n, opp } => {
                let p = if *opp { o } else { me };
                for _ in 0..(*n).min(self.ps(p).life.len()) {
                    let top = *self.ps(p).life.last().unwrap();
                    self.mv(top, Zone::Hand);
                }
            }
            Tpl::DeckToLife { n } => {
                for _ in 0..(*n).min(self.ps(me).deck.len()) {
                    if !self.confirm(me, "Add the top card of your deck to your Life?")? {
                        return Ok(());
                    }
                    let top = *self.ps(me).deck.last().unwrap();
                    self.mv(top, Zone::Life);
                }
            }
            Tpl::AddDon { n, rested } => {
                self.add_don(me, *n, *rested);
            }
            Tpl::SetDonActive { n } => {
                let ps = self.psm(me);
                let k = (*n).min(ps.don_rested);
                ps.don_rested -= k;
                ps.don_active += k;
            }
            Tpl::GiveRestedDon { n, chars, name } => {
                let pool = if *chars { self.leader_and_characters(me) } else { vec![self.ps(me).leader] };
                let cands = pool.into_iter().filter(|&c| name.as_ref().map_or(true, |x| self.name(c) == x)).collect();
                if self.ps(me).don_rested == 0 {
                    return Ok(());
                }
                for c in self.choose_cards(me, cands, 1, "Give rested DON!!", false)? {
                    self.give_don(me, c, *n, true);
                }
            }
            Tpl::Buff { n, v, until, who } => {
                let cands = self.own_candidates(who, me);
                for c in self.choose_cards(me, cands, *n, &format!("+{v} power"), false)? {
                    self.add_power(c, *v, *until, Some(me));
                }
            }
            Tpl::Grant { n, kw, who } => {
                let cands = self.own_candidates(who, me);
                for c in self.choose_cards(me, cands, *n, &format!("Gains [{}]", kw.as_str()), false)? {
                    self.grant_keyword(c, *kw, Until::Turn, Some(me));
                }
            }
            Tpl::LeaderBuff { v, until, types } => {
                let l = self.ps(me).leader;
                if types.is_empty() || types.iter().any(|t| self.d(l).has_type(t)) {
                    self.add_power(l, *v, *until, Some(me));
                }
            }
            Tpl::LeaderKeyword { kw, name } => {
                let l = self.ps(me).leader;
                if name.as_ref().map_or(true, |n| self.name(l) == n) {
                    self.grant_keyword(l, *kw, Until::Turn, Some(me));
                }
            }
            Tpl::LeaderBasePower { v, who, until } => {
                let l = self.ps(me).leader;
                let d = self.d(l);
                if who.starts_with('[') && d.name != who[1..who.len() - 1] {
                    return Ok(());
                }
                if who.starts_with('{') && !d.has_type(&who[1..who.find('}').unwrap()]) {
                    return Ok(());
                }
                if who == "monocolored" && d.colors.len() != 1 {
                    return Ok(());
                }
                self.set_base_power(l, *v, *until, Some(me));
            }
            Tpl::Unblockable => self.grant_keyword(src, Kw::Unblockable, Until::Battle, Some(me)),
            Tpl::PlaySelf => {
                if matches!(self.d(src).category, Category::Character | Category::Stage) {
                    self.play_card(src, false)?;
                }
            }
            Tpl::ActivateOwn { timing } => {
                if let Some(i) = self.d(src).first_effect(*timing) {
                    let body = &self.d(src).effects[i].body;
                    self.run(body, src)?;
                }
            }
            Tpl::Look { n, k, pred, to_trash } => {
                let deck = &self.ps(me).deck;
                let top: Vec<Uid> = deck[deck.len().saturating_sub(*n)..].iter().rev().copied().collect();
                let cands = top.iter().copied().filter(|&c| self.filter_ok(pred, c)).collect();
                let picks = self.choose_cards(me, cands, *k, "Add to hand", false)?;
                for &c in &picks {
                    self.mv(c, Zone::Hand);
                }
                let rest: Vec<Uid> = top.into_iter().filter(|c| !picks.contains(c)).collect();
                if *to_trash {
                    for c in rest {
                        self.mv(c, Zone::Trash);
                    }
                } else {
                    self.place_in_order(me, rest, Zone::Deck, true)?;
                }
            }
            Tpl::Scry { n } => {
                // look at the top N, then put them all back on top or all on the bottom, in any order
                let deck = &self.ps(me).deck;
                let top: Vec<Uid> = deck[deck.len().saturating_sub(*n)..].iter().rev().copied().collect();
                if top.is_empty() {
                    return Ok(());
                }
                let names: Vec<&str> = top.iter().map(|&c| self.name(c)).collect();
                let opts = vec![
                    Action::new(ActKind::Select).index(0).label("Top of your deck"),
                    Action::new(ActKind::Select).index(1).label("Bottom of your deck"),
                ];
                let prompt = format!("Place {} at the top or bottom of your deck?", names.join(", "));
                let bottom = self.choose(me, DecisionKind::Select, opts, &prompt)?.index == Some(1);
                self.place_in_order(me, top, Zone::Deck, bottom)?;
            }
            Tpl::PlayFrom { n, hand, trash, hand_first, rested, pred } => {
                let ps = self.ps(me);
                let mut pool: Vec<Uid> = vec![];
                let zones: Vec<&[Uid]> = match (hand, trash, hand_first) {
                    (true, true, true) => vec![&ps.hand, &ps.trash],
                    (true, true, false) => vec![&ps.trash, &ps.hand],
                    (true, false, _) => vec![&ps.hand],
                    _ => vec![&ps.trash],
                };
                for z in zones {
                    pool.extend_from_slice(z);
                }
                let cands = pool
                    .into_iter()
                    .filter(|&c| {
                        matches!(self.d(c).category, Category::Character | Category::Stage) && self.filter_ok(pred, c)
                    })
                    .collect();
                for c in self.choose_cards(me, cands, *n, "Play a card", false)? {
                    self.play_card(c, *rested)?;
                }
            }
            Tpl::CharToLife { n, opp_only, cost } => {
                let sides: Vec<u8> = if *opp_only { vec![o] } else { vec![o, me] };
                let cands = sides.into_iter().flat_map(|p| self.characters(p)).filter(|&c| self.cost(c) <= *cost).collect();
                for c in self.choose_cards(me, cands, *n, "Add to Life face-up", false)? {
                    let bottom =
                        self.confirm(me, &format!("Put {} at the bottom of the Life cards?", self.name(c)))?;
                    self.remove_from_field(c, Zone::Life, Some(me), bottom, true)?;
                }
            }
            Tpl::HandToLife { n, pred, face_up } => {
                let cands = self.ps(me).hand.iter().copied().filter(|&c| self.filter_ok(pred, c)).collect();
                let prompt = if *face_up { "Add to Life face-up" } else { "Add to Life" };
                for c in self.choose_cards(me, cands, *n, prompt, false)? {
                    self.move_card(c, Some(Zone::Life), false, *face_up);
                }
            }
            Tpl::TrashToHand { n, pred } => {
                let cands = self.ps(me).trash.iter().copied().filter(|&c| self.filter_ok(pred, c)).collect();
                for c in self.choose_cards(me, cands, *n, "Add to hand from trash", false)? {
                    self.mv(c, Zone::Hand);
                }
            }
        }
        Ok(())
    }
}
