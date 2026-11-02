//! Plays whole games with random choices on the three fully implemented decks.
use std::sync::Arc;

use optcg_rs::db::Db;
use optcg_rs::engine::{DeckList, Game};
use optcg_rs::rng::PyRandom;

fn decks() -> Vec<(String, Vec<String>)> {
    let text = std::fs::read_to_string("../data/decks.json").unwrap();
    let all: Vec<serde_json::Value> = serde_json::from_str(&text).unwrap();
    let mut out = vec![];
    for name in ["(Blue) Rocks.D.Xebec", "(Yellow) Charlotte Linlin", "(Black) Monkey.D.Luffy"] {
        let d = all.iter().find(|d| d["name"] == name).unwrap();
        let mut cards = vec![];
        for (id, n) in d["cards"].as_object().unwrap() {
            for _ in 0..n.as_u64().unwrap() {
                cards.push(id.clone());
            }
        }
        out.push((d["leader"].as_str().unwrap().to_string(), cards));
    }
    out
}

fn list(d: &(String, Vec<String>)) -> DeckList<'_> {
    DeckList { leader: &d.0, cards: d.1.iter().map(|s| s.as_str()).collect() }
}

#[test]
fn random_games_finish() {
    let db = Arc::new(Db::load("../data/cards.json").unwrap());
    let decks = decks();
    let mut rng = PyRandom::new(1);
    let mut steps = 0;
    for seed in 0..60u64 {
        let (a, b) = (&decks[seed as usize % 3], &decks[(seed as usize + 1) % 3]);
        let mut g = Game::new(db.clone(), [list(a), list(b)], seed).unwrap();
        while !g.over {
            let n = g.pending.as_ref().unwrap().options.len();
            g.step(rng.randrange(n)).unwrap();
            steps += 1;
            assert!(steps < 1_000_000);
        }
    }
    println!("{steps} decisions");
}

fn midgame(db: &Arc<Db>, decks: &[(String, Vec<String>)], seed: u64, steps: usize, rng: &mut PyRandom) -> Game {
    let mut g = Game::new(db.clone(), [list(&decks[0]), list(&decks[1])], seed).unwrap();
    for _ in 0..steps {
        if g.over {
            break;
        }
        let n = g.pending.as_ref().unwrap().options.len();
        g.step(rng.randrange(n)).unwrap();
    }
    g
}

fn snap(g: &mut Game) -> String {
    g.view(|e| optcg_rs::snapshot(e)).to_string()
}

#[test]
fn clones_are_independent_and_identical() {
    let db = Arc::new(Db::load("../data/cards.json").unwrap());
    let decks = decks();
    for seed in 0..20u64 {
        let mut rng = PyRandom::new(seed + 100);
        let mut g = midgame(&db, &decks, seed, 40 + seed as usize * 3, &mut rng);
        if g.over {
            continue;
        }
        let before = snap(&mut g);
        let (mut a, mut b) = (g.clone(), g.clone());
        let mut r1 = PyRandom::new(seed);
        let mut r2 = PyRandom::new(seed);
        while !a.over {
            let n = a.pending.as_ref().unwrap().options.len();
            a.step(r1.randrange(n)).unwrap();
            b.step(r2.randrange(n)).unwrap();
        }
        assert_eq!(a.log, b.log, "the same moves from two clones give the same game");
        assert_eq!(snap(&mut g), before, "playing a clone doesn't touch the original");
    }
}

#[test]
fn determinize_only_moves_hidden_cards() {
    let db = Arc::new(Db::load("../data/cards.json").unwrap());
    let decks = decks();
    let (mut tried, mut ok) = (0, 0);
    for seed in 0..30u64 {
        let mut rng = PyRandom::new(seed + 7);
        let g = midgame(&db, &decks, seed, 30 + seed as usize * 4, &mut rng);
        if g.over {
            continue;
        }
        let p = g.pending.as_ref().unwrap().player;
        let mut d = g.clone();
        tried += 1;
        if !d.determinize(p, seed) {
            continue;
        }
        ok += 1;
        let (s, t) = (&g.state, &d.state);
        for q in 0..2usize {
            let ids = |st: &optcg_rs::engine::State, v: &[u16]| {
                let mut x: Vec<u16> = v.iter().map(|&u| st.cards[u as usize].def).collect();
                x.sort();
                x
            };
            // what p sees is unchanged
            let field = |st: &optcg_rs::engine::State| {
                st.players[q].field().iter().map(|&u| st.cards[u as usize].def).collect::<Vec<_>>()
            };
            assert_eq!(field(s), field(t));
            assert_eq!(ids(s, &s.players[q].trash), ids(t, &t.players[q].trash));
            if q as u8 == p {
                assert_eq!(s.players[q].hand.iter().map(|&u| s.cards[u as usize].def).collect::<Vec<_>>(),
                           t.players[q].hand.iter().map(|&u| t.cards[u as usize].def).collect::<Vec<_>>());
            }
            // hidden cards are only rearranged among themselves
            let mut all_s = [ids(s, &s.players[q].deck), ids(s, &s.players[q].hand), ids(s, &s.players[q].life)].concat();
            let mut all_t = [ids(t, &t.players[q].deck), ids(t, &t.players[q].hand), ids(t, &t.players[q].life)].concat();
            all_s.sort();
            all_t.sort();
            assert_eq!(all_s, all_t);
        }
        assert_eq!(g.pending.as_ref().unwrap().player, d.pending.as_ref().unwrap().player);
    }
    println!("determinize succeeded {ok}/{tried}");
    assert!(ok * 10 >= tried * 8, "determinize should usually fit: {ok}/{tried}");
}
