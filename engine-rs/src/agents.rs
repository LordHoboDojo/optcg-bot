//! What the training code needs from a game besides the rules: the network's observation
//! (optcg/env.py `encode`) and the greedy baseline (optcg/agents.py `GreedyAgent`). Both give
//! exactly what the Python versions give, so training data doesn't depend on the engine.

use crate::cards::*;
use crate::engine::{Eng, Phase};

pub const CARD_FIELDS: usize = 16;
pub const OPTION_FIELDS: usize = 6;

pub struct Obs {
    pub scalars: Vec<f64>,
    pub cards: Vec<[f64; CARD_FIELDS]>,
    pub options: Vec<[i64; OPTION_FIELDS]>,
    pub decision: usize,
    pub player: u8,
}

const KEYWORD_FIELDS: [Kw; 5] = [Kw::Rush, Kw::DoubleAttack, Kw::Banish, Kw::Blocker, Kw::Unblockable];

/// env.ZONES = (hand, leader, character, stage, trash, life, deck)
fn zone_index(z: Option<Zone>) -> usize {
    match z {
        Some(Zone::Hand) => 0,
        Some(Zone::Leader) => 1,
        Some(Zone::Character) => 2,
        Some(Zone::Stage) => 3,
        Some(Zone::Trash) => 4,
        Some(Zone::Life) => 5,
        _ => 6, // the deck, or no area (a [Trigger] resolving)
    }
}

/// env.PHASES = (setup, refresh, draw, don, main, attack, end_of_battle, end); others are 8.
fn phase_index(p: Phase) -> usize {
    match p {
        Phase::Setup => 0,
        Phase::Refresh => 1,
        Phase::Draw => 2,
        Phase::Don => 3,
        Phase::Main => 4,
        Phase::Attack => 5,
        Phase::EndOfBattle => 6,
        Phase::End => 7,
        _ => 8,
    }
}

fn category_index(c: Category) -> usize {
    match c {
        Category::Leader => 0,
        Category::Character => 1,
        Category::Event => 2,
        Category::Stage => 3,
    }
}

/// The observation for the player who must answer `d` (env.encode).
pub fn encode(e: &Eng, d: &Decision) -> Obs {
    let me = d.player;
    let mut rows: Vec<[f64; CARD_FIELDS]> = Vec::with_capacity(48);
    let mut row_of: Vec<i64> = vec![-1; e.s.cards.len()];
    let battle = e.s.battle.as_ref().map(|b| (b.attacker, b.target));

    let add = |u: Uid, zone: Option<Zone>, rows: &mut Vec<[f64; CARD_FIELDS]>, row_of: &mut Vec<i64>| {
        let c = e.c(u);
        let def = e.d(u);
        let on_field = matches!(zone, Some(Zone::Leader) | Some(Zone::Character) | Some(Zone::Stage));
        let powered = matches!(def.category, Category::Leader | Category::Character);
        let power = if on_field && powered { e.power(u) } else { def.power };
        let cost = if def.category != Category::Leader { e.cost(u) } else { 0 };
        let counter = if zone == Some(Zone::Hand) { e.counter_value(u) } else { def.counter };
        let mut row = [0f64; CARD_FIELDS];
        row[0] = e.db.emb[c.def as usize] as f64;
        row[1] = (c.owner == me) as u8 as f64;
        row[2] = zone_index(zone) as f64;
        row[3] = category_index(def.category) as f64;
        row[4] = power as f64 / 1000.0;
        row[5] = cost as f64;
        row[6] = counter as f64 / 1000.0;
        row[7] = c.rested as u8 as f64;
        row[8] = c.don as f64;
        row[9] = (c.played_turn == e.s.turn_number && on_field) as u8 as f64;
        row[10] = battle.map_or(false, |(a, t)| u == a || u == t) as u8 as f64;
        for (k, kw) in KEYWORD_FIELDS.iter().enumerate() {
            row[11 + k] = (on_field && e.has_keyword(u, *kw)) as u8 as f64;
        }
        row_of[u as usize] = rows.len() as i64;
        rows.push(row);
    };

    for p in [me, 1 - me] {
        let ps = e.ps(p);
        add(ps.leader, Some(Zone::Leader), &mut rows, &mut row_of);
        for &u in &ps.characters {
            add(u, Some(Zone::Character), &mut rows, &mut row_of);
        }
        if let Some(u) = ps.stage {
            add(u, Some(Zone::Stage), &mut rows, &mut row_of);
        }
        for &u in &ps.trash {
            add(u, Some(Zone::Trash), &mut rows, &mut row_of);
        }
        for &u in &ps.life {
            if e.c(u).face_up {
                add(u, Some(Zone::Life), &mut rows, &mut row_of);
            }
        }
        for &u in &ps.hand {
            if p == me || e.c(u).revealed {
                // the opponent's hand only where a card was shown
                add(u, Some(Zone::Hand), &mut rows, &mut row_of);
            }
        }
    }
    // cards an effect lets this player see (deck searches, revealed cards) appear only in options
    for o in &d.options {
        for u in [o.card, o.target].into_iter().flatten() {
            if row_of[u as usize] < 0 {
                add(u, e.c(u).zone, &mut rows, &mut row_of);
            }
        }
    }

    let options = d
        .options
        .iter()
        .map(|o| {
            let emb = |u: Option<Uid>| u.map_or(0, |u| e.db.emb[e.c(u).def as usize] as i64);
            [
                o.kind as i64,
                o.card.map_or(-1, |u| row_of[u as usize]),
                o.target.map_or(-1, |u| row_of[u as usize]),
                o.index.map_or(-1, |i| i as i64),
                emb(o.card),
                emb(o.target),
            ]
        })
        .collect();

    let mut scalars = Vec::with_capacity(20);
    for p in [me, 1 - me] {
        let ps = e.ps(p);
        scalars.extend([
            ps.life.len() as f64,
            ps.hand.len() as f64,
            ps.deck.len() as f64,
            ps.trash.len() as f64,
            ps.don_active as f64,
            ps.don_rested as f64,
            ps.don_deck as f64,
            ps.characters.len() as f64,
        ]);
    }
    scalars.extend([
        (e.s.turn_player == me) as u8 as f64,
        e.s.turn_number as f64,
        phase_index(e.s.phase) as f64,
        e.s.battle.is_some() as u8 as f64,
    ]);
    Obs { scalars, cards: rows, options, decision: d.kind as usize, player: me }
}

// ---------------------------------------------------------------- GreedyAgent

/// First option of a kind (agents._find).
fn find(d: &Decision, k: ActKind) -> Option<usize> {
    d.options.iter().position(|o| o.kind == k)
}

/// Python's max(): the FIRST of the largest. (Rust's max_by_key keeps the last.)
fn first_max(items: impl Iterator<Item = (usize, i32)>) -> Option<usize> {
    let mut best: Option<(usize, i32)> = None;
    for (i, v) in items {
        if best.map_or(true, |(_, b)| v > b) {
            best = Some((i, v));
        }
    }
    best.map(|(i, _)| i)
}

/// Python's min(): the first of the smallest.
fn first_min(items: impl Iterator<Item = (usize, i32)>) -> Option<usize> {
    let mut best: Option<(usize, i32)> = None;
    for (i, v) in items {
        if best.map_or(true, |(_, b)| v < b) {
            best = Some((i, v));
        }
    }
    best.map(|(i, _)| i)
}

/// agents.GreedyAgent: the option it would pick, as an index into `d.options`.
pub fn greedy(e: &Eng, d: &Decision) -> usize {
    let choice = match d.kind {
        DecisionKind::GoFirst => find(d, ActKind::First),
        DecisionKind::Mulligan => find(d, ActKind::Keep),
        DecisionKind::Trigger | DecisionKind::Confirm => find(d, ActKind::Yes),
        DecisionKind::Select => greedy_select(e, d),
        DecisionKind::Main => greedy_main(e, d),
        DecisionKind::Block => greedy_block(e, d),
        DecisionKind::Counter => greedy_counter(e, d),
        _ => None,
    };
    choice.unwrap_or_else(|| greedy_default(e, d))
}

fn strength(e: &Eng, u: Uid) -> i32 {
    let d = e.d(u);
    if matches!(d.category, Category::Leader | Category::Character) {
        e.power(u)
    } else {
        d.cost * 1000
    }
}

/// Effect choices: harm the opponent's strongest card, help our own strongest card.
fn greedy_default(e: &Eng, d: &Decision) -> usize {
    let cards: Vec<usize> = (0..d.options.len()).filter(|&i| d.options[i].card.is_some()).collect();
    if cards.is_empty() {
        return 0;
    }
    let theirs: Vec<usize> = cards.iter().copied().filter(|&i| e.owner(d.options[i].card.unwrap()) != d.player).collect();
    let pool = if theirs.is_empty() { cards } else { theirs };
    first_max(pool.into_iter().map(|i| (i, strength(e, d.options[i].card.unwrap())))).unwrap()
}

fn greedy_select(e: &Eng, d: &Decision) -> Option<usize> {
    if !d.prompt.contains("Trash") {
        return None;
    }
    // discard the cheapest card (printed cost)
    first_min(
        d.options.iter().enumerate().filter_map(|(i, o)| o.card.map(|u| (i, e.d(u).cost))),
    )
}

fn greedy_main(e: &Eng, d: &Decision) -> Option<usize> {
    let me = d.player;
    let (ps, opp) = (e.ps(me), e.ps(1 - me));
    let of = |k: ActKind| (0..d.options.len()).filter(move |&i| d.options[i].kind == k);
    let card = |i: usize| d.options[i].card.unwrap();
    let target = |i: usize| d.options[i].target.unwrap();
    let cat = |i: usize| e.d(card(i)).category;

    let chars: Vec<usize> = of(ActKind::Play).filter(|&i| cat(i) == Category::Character).collect();
    if !chars.is_empty() && ps.characters.len() < 5 {
        return first_max(chars.into_iter().map(|i| (i, e.cost(card(i)))));
    }
    if let Some(i) = of(ActKind::Play).find(|&i| cat(i) == Category::Event) {
        if !opp.characters.is_empty() {
            return Some(i);
        }
    }
    if let Some(i) = of(ActKind::Play).find(|&i| cat(i) == Category::Stage) {
        if ps.stage.is_none() {
            return Some(i);
        }
    }
    if let Some(i) = of(ActKind::Activate).next() {
        return Some(i);
    }
    // Spend leftover DON!! on the leader so it can swing harder.
    if of(ActKind::GiveDon).next().is_some() && !e.c(ps.leader).rested && e.s.turn_number > 2 {
        return of(ActKind::GiveDon).find(|&i| target(i) == ps.leader);
    }
    let attacks: Vec<usize> = of(ActKind::Attack).collect();
    let good: Vec<usize> = attacks.iter().copied().filter(|&i| e.power(card(i)) >= e.power(target(i))).collect();
    let on_leader: Vec<usize> = good.iter().copied().filter(|&i| target(i) == opp.leader).collect();
    if !on_leader.is_empty() {
        return first_max(on_leader.into_iter().map(|i| (i, e.power(card(i)))));
    }
    if !good.is_empty() {
        return first_max(good.into_iter().map(|i| (i, e.cost(target(i)))));
    }
    // Nothing wins outright: still swing at the Leader, for chip damage or to spend Counters.
    let chip: Vec<usize> = attacks.into_iter().filter(|&i| target(i) == opp.leader).collect();
    if !chip.is_empty() {
        return first_max(chip.into_iter().map(|i| (i, e.power(card(i)))));
    }
    find(d, ActKind::End)
}

fn greedy_block(e: &Eng, d: &Decision) -> Option<usize> {
    let b = e.s.battle.as_ref()?;
    let life = e.ps(d.player).life.len();
    let blocks: Vec<usize> = (0..d.options.len()).filter(|&i| d.options[i].kind == ActKind::Block).collect();
    let card = |i: usize| d.options[i].card.unwrap();
    if let Some(&i) = blocks.iter().find(|&&i| e.power(card(i)) > e.power(b.attacker)) {
        return Some(i);
    }
    if e.d(b.target).category == Category::Leader && life <= 2 && !blocks.is_empty() {
        return first_min(blocks.into_iter().map(|i| (i, e.cost(card(i)))));
    }
    find(d, ActKind::Pass)
}

fn greedy_counter(e: &Eng, d: &Decision) -> Option<usize> {
    let b = e.s.battle.as_ref()?;
    let need = e.power(b.attacker) - e.power(b.target) + 1;
    if need <= 0 {
        return find(d, ActKind::Pass);
    }
    let life = e.ps(d.player).life.len();
    let tcat = e.d(b.target).category;
    let worth_it = (tcat == Category::Character && e.cost(b.target) >= 4) || (tcat == Category::Leader && life <= 3);
    let counters: Vec<usize> = (0..d.options.len())
        .filter(|&i| d.options[i].kind == ActKind::Counter && d.options[i].target == Some(b.target))
        .collect();
    let value = |i: usize| e.counter_value(d.options[i].card.unwrap());
    let total: i32 = counters.iter().map(|&i| value(i)).sum();
    if !worth_it || total < need {
        return find(d, ActKind::Pass);
    }
    let enough: Vec<usize> = counters.iter().copied().filter(|&i| value(i) >= need).collect();
    if !enough.is_empty() {
        return first_min(enough.into_iter().map(|i| (i, value(i))));
    }
    first_max(counters.into_iter().map(|i| (i, value(i))))
}
