//! Cards read by the second compiler (compile2.rs), played on boards set up for them.
use std::sync::Arc;

use optcg_rs::cards::{ActKind, Action, DecisionKind, Uid, Zone};
use optcg_rs::db::Db;
use optcg_rs::engine::{DeckList, Game};

const FILLER: &str = "EB01-005"; // a red Character with no effect

fn db() -> Arc<Db> {
    Arc::new(Db::load("../data/cards.json").unwrap())
}

/// A game where each player's deck holds the given cards plus filler, at player 0's Main
/// Phase on turn 3 (so both may attack).
fn game(db: &Arc<Db>, cards: [&[&str]; 2]) -> Game {
    game_with(db, ["ST01-001", "ST01-001"], cards)
}

fn game_with(db: &Arc<Db>, leaders: [&str; 2], cards: [&[&str]; 2]) -> Game {
    let lists: Vec<Vec<&str>> = cards
        .iter()
        .map(|c| {
            let mut v: Vec<&str> = c.to_vec();
            v.resize(50, FILLER);
            v
        })
        .collect();
    let decks = [
        DeckList { leader: leaders[0], cards: lists[0].clone() },
        DeckList { leader: leaders[1], cards: lists[1].clone() },
    ];
    let mut g = Game::new(db.clone(), decks, 1).unwrap();
    loop {
        let d = g.pending.as_ref().unwrap();
        if d.kind == DecisionKind::Main && g.state.turn_number >= 3 && d.player == 0 {
            return g;
        }
        let i = match d.kind {
            DecisionKind::GoFirst => {
                let me_first = d.player == 0;
                d.options.iter().position(|o| (o.kind == ActKind::First) == me_first).unwrap()
            }
            DecisionKind::Main => d.options.iter().position(|o| o.kind == ActKind::End).unwrap(),
            _ => 0,
        };
        g.step(i).unwrap();
    }
}

/// Player p's copy of card `id` (the first one not yet used in `taken`).
fn card(g: &Game, p: u8, id: &str, taken: &mut Vec<Uid>) -> Uid {
    let def = g.db.index(id).unwrap();
    let u = g.state.cards.iter().find(|c| c.owner == p && c.def == def && !taken.contains(&c.uid)).unwrap().uid;
    taken.push(u);
    u
}

fn pick(g: &mut Game, f: impl Fn(&Action) -> bool) {
    let i = g.pending.as_ref().unwrap().options.iter().position(f).expect("no such option");
    g.step(i).unwrap();
}

fn has(g: &Game, f: impl Fn(&Action) -> bool) -> bool {
    g.pending.as_ref().map_or(false, |d| d.options.iter().any(f))
}

fn zone(g: &Game, u: Uid) -> Option<Zone> {
    g.state.cards[u as usize].zone
}

#[test]
fn condition_counts_rested_characters() {
    let db = db();
    for rested in [1, 2] {
        let mut g = game(&db, [&["OP09-026"], &[]]);
        let mut t = vec![];
        let saka = card(&g, 0, "OP09-026", &mut t);
        let mine: Vec<Uid> = (0..2).map(|_| card(&g, 0, FILLER, &mut t)).collect();
        let theirs = card(&g, 1, FILLER, &mut t);
        g.arrange(|e| {
            e.mv(saka, Zone::Hand);
            for (i, &u) in mine.iter().enumerate() {
                e.mv(u, Zone::Character);
                e.cm(u).rested = i < rested;
            }
            e.mv(theirs, Zone::Character);
            e.psm(0).don_active = 6;
        });
        pick(&mut g, |o| o.kind == ActKind::Play && o.card == Some(saka));
        if rested == 2 {
            pick(&mut g, |o| o.card == Some(theirs));
            assert_eq!(zone(&g, theirs), Some(Zone::Trash));
        } else {
            assert_eq!(zone(&g, theirs), Some(Zone::Character));
        }
    }
}

#[test]
fn replacement_trashes_an_event_instead_of_the_ko() {
    let db = db();
    let mut g = game(&db, [&["OP09-026"], &["OP15-014", "ST03-015"]]);
    let mut t = vec![];
    let saka = card(&g, 0, "OP09-026", &mut t);
    let bart = card(&g, 1, "OP15-014", &mut t);
    let event = card(&g, 1, "ST03-015", &mut t);
    let mine: Vec<Uid> = (0..2).map(|_| card(&g, 0, FILLER, &mut t)).collect();
    g.arrange(|e| {
        e.mv(saka, Zone::Hand);
        for &u in &mine {
            e.mv(u, Zone::Character);
            e.cm(u).rested = true;
        }
        e.mv(bart, Zone::Character);
        e.mv(event, Zone::Hand);
        e.psm(0).don_active = 6;
    });
    pick(&mut g, |o| o.kind == ActKind::Play && o.card == Some(saka));
    pick(&mut g, |o| o.card == Some(bart));
    // player 1 is offered the replacement and takes it
    assert_eq!(g.pending.as_ref().unwrap().player, 1);
    pick(&mut g, |o| o.kind == ActKind::Yes);
    if has(&g, |o| o.card == Some(event)) {
        pick(&mut g, |o| o.card == Some(event));
    }
    assert_eq!(zone(&g, bart), Some(Zone::Character));
    assert_eq!(zone(&g, event), Some(Zone::Trash));
}

#[test]
fn static_power_for_a_type_on_the_opponents_turn() {
    let db = db();
    let mut g = game(&db, [&["EB03-041", "EB03-008"], &[]]);
    let mut t = vec![];
    let kuj = card(&g, 0, "EB03-041", &mut t);
    let hib = card(&g, 0, "EB03-008", &mut t);
    g.arrange(|e| {
        e.mv(kuj, Zone::Character);
        e.mv(hib, Zone::Character);
    });
    assert_eq!(g.view(|e| e.power(hib)), 5000);
    g.arrange(|e| e.s.turn_player = 1);
    assert_eq!(g.view(|e| (e.power(hib), e.power(kuj))), (7000, 8000));
}

#[test]
fn can_also_attack_active_characters() {
    let db = db();
    let mut g = game(&db, [&["EB03-041", "EB03-008"], &[]]);
    let mut t = vec![];
    let kuj = card(&g, 0, "EB03-041", &mut t);
    let hib = card(&g, 0, "EB03-008", &mut t);
    let theirs = card(&g, 1, FILLER, &mut t);
    g.arrange(|e| {
        e.mv(kuj, Zone::Character);
        e.mv(hib, Zone::Hand);
        e.mv(theirs, Zone::Character);
        e.psm(0).don_active = 3;
    });
    let attack = |o: &Action| o.kind == ActKind::Attack && o.card == Some(kuj) && o.target == Some(theirs);
    assert!(!has(&g, attack));
    pick(&mut g, |o| o.kind == ActKind::Play && o.card == Some(hib));
    pick(&mut g, |o| o.card == Some(kuj));
    assert!(has(&g, attack));
}

#[test]
fn rest_one_of_your_cards_can_rest_don() {
    let db = db();
    let mut g = game(&db, [&["ST03-015"], &["OP14-029"]]);
    let mut t = vec![];
    let ev = card(&g, 0, "ST03-015", &mut t);
    let tashigi = card(&g, 1, "OP14-029", &mut t);
    g.arrange(|e| {
        e.mv(ev, Zone::Hand);
        e.mv(tashigi, Zone::Character);
        e.psm(0).don_active = 4;
        e.psm(1).don_active = 1;
        let l = e.ps(1).leader;
        e.cm(l).rested = true;
    });
    pick(&mut g, |o| o.kind == ActKind::Play && o.card == Some(ev));
    pick(&mut g, |o| o.card == Some(tashigi));
    pick(&mut g, |o| o.kind == ActKind::Yes);
    // Tashigi stays; the cost is paid by resting the active DON!! (Tashigi can rest itself too)
    let d = g.pending.as_ref().unwrap();
    assert_eq!(d.player, 1);
    assert!(d.options.iter().any(|o| o.card.is_none() && o.index == Some(0)));
    pick(&mut g, |o| o.card.is_none() && o.index == Some(0));
    assert_eq!(zone(&g, tashigi), Some(Zone::Character));
    assert_eq!(g.state.players[1].don_rested, 1);
}

/// Answer every pending decision of `p` that isn't `want` with its first option (no block, no
/// counter), until `want` is asked or the game ends.
fn pass_until(g: &mut Game, want: impl Fn(&optcg_rs::cards::Decision) -> bool) {
    while let Some(d) = g.pending.as_ref() {
        if want(d) {
            return;
        }
        let i = d.options.iter().position(|o| o.kind == ActKind::Pass).unwrap_or(0);
        g.step(i).unwrap();
    }
}

#[test]
fn cannot_be_ko_in_battle_by_strike_cards() {
    let db = db();
    for given in [0, 1] {
        let mut g = game(&db, [&["EB02-001"], &["P-054"]]);
        let mut t = vec![];
        let striker = card(&g, 0, "EB02-001", &mut t); // 7000, <Strike>
        let garp = card(&g, 1, "P-054", &mut t); // 7000, [DON!! x1] no K.O. in battle by <Strike>
        g.arrange(|e| {
            e.mv(striker, Zone::Character);
            e.mv(garp, Zone::Character);
            e.cm(garp).rested = true;
            e.cm(garp).don = given;
        });
        pick(&mut g, |o| o.kind == ActKind::Attack && o.card == Some(striker) && o.target == Some(garp));
        pass_until(&mut g, |d| d.kind == DecisionKind::Main && d.player == 0);
        let want = if given == 1 { Some(Zone::Character) } else { Some(Zone::Trash) };
        assert_eq!(zone(&g, garp), want, "DON!! given: {given}");
    }
}

#[test]
fn set_power_to_zero() {
    let db = db();
    let mut g = game(&db, [&["EB04-010"], &[]]);
    let mut t = vec![];
    let stage = card(&g, 0, "EB04-010", &mut t);
    let theirs = card(&g, 1, FILLER, &mut t);
    g.arrange(|e| {
        e.mv(stage, Zone::Hand);
        e.mv(theirs, Zone::Character);
        e.psm(0).don_active = 7;
    });
    pick(&mut g, |o| o.kind == ActKind::Play && o.card == Some(stage));
    pick(&mut g, |o| o.card == Some(theirs));
    assert_eq!(g.view(|e| e.power(theirs)), 0);
}

#[test]
fn choose_a_cost_and_reveal() {
    let db = db();
    for right in [true, false] {
        let mut g = game(&db, [&["OP11-081"], &[]]);
        let mut t = vec![];
        let ev = card(&g, 0, "OP11-081", &mut t);
        let theirs = card(&g, 1, FILLER, &mut t);
        g.arrange(|e| {
            e.mv(ev, Zone::Hand);
            e.mv(theirs, Zone::Character);
            e.psm(0).don_active = 6;
        });
        let top = *g.state.players[1].deck.last().unwrap();
        let cost = g.db.defs[g.state.cards[top as usize].def as usize].cost;
        let guess = if right { cost } else { (cost + 1) % 11 };
        pick(&mut g, |o| o.kind == ActKind::Play && o.card == Some(ev));
        pick(&mut g, |o| o.index == Some(guess));
        if right {
            pick(&mut g, |o| o.card == Some(theirs));
            assert_eq!(zone(&g, theirs), Some(Zone::Trash));
        } else {
            assert_eq!(zone(&g, theirs), Some(Zone::Character));
        }
    }
}

#[test]
fn empty_deck_loses_at_end_of_turn_with_brook() {
    let db = db();
    for leader in ["OP15-022", "ST01-001"] {
        let mut g = game_with(&db, [leader, "ST01-001"], [&[], &[]]);
        g.arrange(|e| {
            for c in e.ps(0).deck.to_vec() {
                e.mv(c, Zone::Trash);
            }
        });
        if leader == "ST01-001" {
            assert!(g.over && g.winner == Some(1), "an empty deck loses at once");
            continue;
        }
        assert!(!g.over, "Brook: not yet");
        pick(&mut g, |o| o.kind == ActKind::End);
        assert!(g.over && g.winner == Some(1), "Brook: at the end of the turn");
    }
}

#[test]
fn ko_up_to_two_with_a_total_power() {
    let db = db();
    let mut g = game(&db, [&["OP05-007"], &[]]);
    let mut t = vec![];
    let ko = card(&g, 0, "OP05-007", &mut t);
    let a = card(&g, 1, FILLER, &mut t); // 3000 power each
    let b = card(&g, 1, FILLER, &mut t);
    g.arrange(|e| {
        e.mv(ko, Zone::Hand);
        e.mv(a, Zone::Character);
        e.mv(b, Zone::Character);
        e.psm(0).don_active = 10;
    });
    pick(&mut g, |o| o.kind == ActKind::Play && o.card == Some(ko));
    pick(&mut g, |o| o.card == Some(a));
    // 1000 left of the 4000: the other 3000-power Character can't be chosen too
    assert!(!has(&g, |o| o.card == Some(b) && o.kind == ActKind::Select));
    assert_eq!(zone(&g, a), Some(Zone::Trash));
    assert_eq!(zone(&g, b), Some(Zone::Character));
}
