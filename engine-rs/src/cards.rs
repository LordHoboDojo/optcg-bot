//! Card definitions (printed data), card instances, and what players are asked and answer.
//! Mirrors optcg/cards.py and optcg/actions.py.

use smallvec::SmallVec;

use crate::effects::{Body, Cond, KoFilterFn, Statics};

pub type Uid = u16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Category {
    Leader,
    Character,
    Event,
    Stage,
}

impl Category {
    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "Leader" => Self::Leader,
            "Character" => Self::Character,
            "Event" => Self::Event,
            "Stage" => Self::Stage,
            _ => return None,
        })
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Leader => "Leader",
            Self::Character => "Character",
            Self::Event => "Event",
            Self::Stage => "Stage",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Timing {
    OnPlay,
    WhenAttacking,
    OnOppAttack,
    OnBlock,
    OnKo,
    EndOfYourTurn,
    EndOfOppTurn,
    ActivateMain,
    Main,
    Counter,
    Trigger,
    StartOfGame,
    StartOfTurn,
    StartOfMain,
    EndOfBattle,
    AttackDeclared,
    DonReturned,
    /// "when one of your Characters ... is K.O.'d" (Effect::ko_filter)
    AllyKo,
}

impl Timing {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::OnPlay => "On Play",
            Self::WhenAttacking => "When Attacking",
            Self::OnOppAttack => "On Your Opponent's Attack",
            Self::OnBlock => "On Block",
            Self::OnKo => "On K.O.",
            Self::EndOfYourTurn => "End of Your Turn",
            Self::EndOfOppTurn => "End of Your Opponent's Turn",
            Self::ActivateMain => "Activate: Main",
            Self::Main => "Main",
            Self::Counter => "Counter",
            Self::Trigger => "Trigger",
            Self::StartOfGame => "Start of Game",
            Self::StartOfTurn => "Start of Turn",
            Self::StartOfMain => "Start of Main Phase",
            Self::EndOfBattle => "End of Battle",
            Self::AttackDeclared => "Attack Declared",
            Self::DonReturned => "DON!! Returned",
            Self::AllyKo => "Ally K.O.",
        }
    }
    /// Printed timing tags (effect_text.TIMING_TAGS).
    pub fn from_tag(tag: &str) -> Option<Self> {
        Some(match tag {
            "On Play" => Self::OnPlay,
            "When Attacking" => Self::WhenAttacking,
            "On K.O." => Self::OnKo,
            "On Block" => Self::OnBlock,
            "Activate: Main" => Self::ActivateMain,
            "Main" => Self::Main,
            "Counter" => Self::Counter,
            "Trigger" => Self::Trigger,
            "End of Your Turn" => Self::EndOfYourTurn,
            "End of Your Opponent's Turn" => Self::EndOfOppTurn,
            "On Your Opponent's Attack" => Self::OnOppAttack,
            _ => return None,
        })
    }
}

/// Keywords (10-1) and the not-printed markers effects grant (10-2-1-3).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kw {
    Rush,
    RushCharacter,
    DoubleAttack,
    Banish,
    Blocker,
    Unblockable,
    CannotAttack,
    NoKoInBattle,
    NoKoByEffects,
    NoKoByOppEffects,
    NoBlocker,
}

impl Kw {
    /// Printed keywords only (cards.KEYWORDS).
    pub fn printed(s: &str) -> Option<Self> {
        Some(match s {
            "Rush" => Self::Rush,
            "Rush: Character" => Self::RushCharacter,
            "Double Attack" => Self::DoubleAttack,
            "Banish" => Self::Banish,
            "Blocker" => Self::Blocker,
            "Unblockable" => Self::Unblockable,
            _ => return None,
        })
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Rush => "Rush",
            Self::RushCharacter => "Rush: Character",
            Self::DoubleAttack => "Double Attack",
            Self::Banish => "Banish",
            Self::Blocker => "Blocker",
            Self::Unblockable => "Unblockable",
            Self::CannotAttack => "Cannot Attack",
            Self::NoKoInBattle => "Cannot be K.O.'d in battle",
            Self::NoKoByEffects => "Cannot be K.O.'d by effects",
            Self::NoKoByOppEffects => "Cannot be K.O.'d by your opponent's effects",
            Self::NoBlocker => "Cannot activate [Blocker]",
        }
    }
    pub const PRINTED: [Kw; 6] = [Kw::Banish, Kw::Blocker, Kw::DoubleAttack, Kw::Rush, Kw::RushCharacter, Kw::Unblockable];
}

#[derive(Clone)]
pub struct Effect {
    pub timing: Timing,
    pub body: Body,
    pub text: String,
    pub once_per_turn: bool,
    pub don_req: u32,
    pub your_turn: bool,
    pub opp_turn: bool,
    pub rest_don: u32,
    pub don_minus: u32,
    pub rest_self: bool,
    pub condition: Option<Cond>,
    pub optional: bool,
    /// Effects printed as one ("[When Attacking]/[On Your Opponent's Attack]") share
    /// [Once Per Turn]; they all carry the index of the first of them.
    pub opt_group: Option<usize>,
    /// AllyKo only: (source, K.O.'d card) -> sets the effect off. Read while the card is on the field.
    pub ko_filter: Option<KoFilterFn>,
}

impl Effect {
    pub fn new(timing: Timing, body: Body, text: &str) -> Self {
        Effect {
            timing,
            body,
            text: text.to_string(),
            once_per_turn: false,
            don_req: 0,
            your_turn: false,
            opp_turn: false,
            rest_don: 0,
            don_minus: 0,
            rest_self: false,
            condition: None,
            optional: false,
            opt_group: None,
            ko_filter: None,
        }
    }
}

#[derive(Clone)]
pub struct CardDef {
    pub id: String,
    pub name: String,
    pub category: Category,
    pub colors: Vec<String>,
    pub cost: i32,
    pub power: i32,
    pub counter: i32,
    pub life: i32,
    pub types: Vec<String>,
    pub attributes: Vec<String>,
    pub keywords: Vec<Kw>,
    pub effects: Vec<Effect>,
    /// Permanent effects and replacement effects (compiled or scripted).
    pub st: Statics,
    pub text: String,
    pub trigger_text: String,
    pub unimplemented: Vec<String>,
}

impl CardDef {
    pub fn effect_indexes(&self, timing: Timing) -> impl Iterator<Item = usize> + '_ {
        self.effects.iter().enumerate().filter(move |(_, e)| e.timing == timing).map(|(i, _)| i)
    }
    pub fn first_effect(&self, timing: Timing) -> Option<usize> {
        self.effect_indexes(timing).next()
    }
    pub fn has_type(&self, t: &str) -> bool {
        self.types.iter().any(|x| x == t)
    }
    pub fn type_includes(&self, t: &str) -> bool {
        self.types.iter().any(|x| x.contains(t))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Until {
    Battle,
    Turn,
    NextTurn,
    NextEnd,
    Permanent,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ModKind {
    Power(i32),
    BasePower(i32),
    Cost(i32),
    Keyword(Kw),
    NoRefresh,
    Negated,
}

#[derive(Clone, Copy, Debug)]
pub struct Mod {
    pub kind: ModKind,
    pub until: Until,
    pub player: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Zone {
    Leader,
    Deck,
    Hand,
    Trash,
    Life,
    Character,
    Stage,
}

/// A physical card in a game. `incarnation` changes every time it changes zones (3-1-6).
#[derive(Clone, Debug)]
pub struct Card {
    pub uid: Uid,
    pub def: u16,
    pub owner: u8,
    pub zone: Option<Zone>,
    pub face_up: bool,
    pub incarnation: u32,
    pub rested: bool,
    pub don: u32,
    pub played_turn: i32,
    pub mods: SmallVec<[Mod; 2]>,
    /// [Once Per Turn]: (effect index or group, turn number).
    pub opt_used: SmallVec<[(usize, i32); 2]>,
}

impl Card {
    pub fn reset(&mut self) {
        self.incarnation += 1;
        self.face_up = false;
        self.rested = false;
        self.played_turn = -1;
        self.mods.clear();
        self.opt_used.clear();
    }
    pub fn opt_turn(&self, key: usize) -> Option<i32> {
        self.opt_used.iter().find(|(k, _)| *k == key).map(|(_, t)| *t)
    }
    pub fn set_opt(&mut self, key: usize, turn: i32) {
        match self.opt_used.iter_mut().find(|(k, _)| *k == key) {
            Some(e) => e.1 = turn,
            None => self.opt_used.push((key, turn)),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActKind {
    Play,
    Activate,
    GiveDon,
    Attack,
    End,
    Block,
    Counter,
    Event,
    Pass,
    Select,
    Yes,
    No,
    First,
    Second,
    Keep,
    Mulligan,
}

impl ActKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Play => "play",
            Self::Activate => "activate",
            Self::GiveDon => "give_don",
            Self::Attack => "attack",
            Self::End => "end",
            Self::Block => "block",
            Self::Counter => "counter",
            Self::Event => "event",
            Self::Pass => "pass",
            Self::Select => "select",
            Self::Yes => "yes",
            Self::No => "no",
            Self::First => "first",
            Self::Second => "second",
            Self::Keep => "keep",
            Self::Mulligan => "mulligan",
        }
    }
}

/// One option of a Decision. Equality ignores the label, as in Python.
#[derive(Clone, Debug)]
pub struct Action {
    pub kind: ActKind,
    pub card: Option<Uid>,
    pub target: Option<Uid>,
    pub index: Option<i32>,
    pub label: String,
}

impl PartialEq for Action {
    fn eq(&self, o: &Self) -> bool {
        self.kind == o.kind && self.card == o.card && self.target == o.target && self.index == o.index
    }
}

impl Action {
    pub fn new(kind: ActKind) -> Self {
        Action { kind, card: None, target: None, index: None, label: String::new() }
    }
    pub fn card(mut self, c: Uid) -> Self {
        self.card = Some(c);
        self
    }
    pub fn target(mut self, t: Uid) -> Self {
        self.target = Some(t);
        self
    }
    pub fn index(mut self, i: i32) -> Self {
        self.index = Some(i);
        self
    }
    pub fn label(mut self, l: impl Into<String>) -> Self {
        self.label = l.into();
        self
    }
    /// `str(action)`: the label, or the kind when there is none.
    pub fn text(&self) -> &str {
        if self.label.is_empty() {
            self.kind.as_str()
        } else {
            &self.label
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecisionKind {
    Main,
    Block,
    Counter,
    Trigger,
    Select,
    Confirm,
    ChooseOne,
    Cost,
    GoFirst,
    Mulligan,
    Order,
    OrderCards,
    ReturnDon,
}

impl DecisionKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Main => "main",
            Self::Block => "block",
            Self::Counter => "counter",
            Self::Trigger => "trigger",
            Self::Select => "select",
            Self::Confirm => "confirm",
            Self::ChooseOne => "choose_one",
            Self::Cost => "cost",
            Self::GoFirst => "go_first",
            Self::Mulligan => "mulligan",
            Self::Order => "order",
            Self::OrderCards => "order_cards",
            Self::ReturnDon => "return_don",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Decision {
    pub player: u8,
    pub kind: DecisionKind,
    pub options: Vec<Action>,
    pub prompt: String,
}
