//! Speed of the Rust engine: random games, and copying a game in progress.
//!     cargo run --release --example bench
use std::sync::Arc;
use std::time::Instant;

use optcg_rs::db::Db;
use optcg_rs::engine::{DeckList, Game};
use optcg_rs::rng::PyRandom;

fn main() {
    let db = Arc::new(Db::load("../data/cards.json").unwrap());
    let text = std::fs::read_to_string("../data/decks.json").unwrap();
    let all: Vec<serde_json::Value> = serde_json::from_str(&text).unwrap();
    let deck = |name: &str| {
        let d = all.iter().find(|d| d["name"] == name).unwrap();
        let cards: Vec<String> = d["cards"]
            .as_object()
            .unwrap()
            .iter()
            .flat_map(|(id, n)| std::iter::repeat(id.clone()).take(n.as_u64().unwrap() as usize))
            .collect();
        (d["leader"].as_str().unwrap().to_string(), cards)
    };
    let (a, b) = (deck("(Blue) Rocks.D.Xebec"), deck("(Yellow) Charlotte Linlin"));
    let lists = || [DeckList { leader: &a.0, cards: a.1.iter().map(|s| s.as_str()).collect() },
                    DeckList { leader: &b.0, cards: b.1.iter().map(|s| s.as_str()).collect() }];

    for (logging, labels) in [(true, true), (false, false)] {
        let mut rng = PyRandom::new(1);
        let (mut games, mut steps) = (0, 0);
        let t = Instant::now();
        while t.elapsed().as_secs_f64() < 3.0 {
            let mut g = Game::new(db.clone(), lists(), games).unwrap();
            g.logging = logging;
            g.labels = labels;
            while !g.over {
                let n = g.pending.as_ref().unwrap().options.len();
                g.step(rng.randrange(n)).unwrap();
                steps += 1;
            }
            games += 1;
        }
        let s = t.elapsed().as_secs_f64();
        println!("random play, log+labels {logging:5}: {:7.0} games/s, {:9.0} decisions/s, {:5.1} us/decision",
                 games as f64 / s, steps as f64 / s, s * 1e6 / steps as f64);
    }

    let mut rng = PyRandom::new(2);
    let mut g = Game::new(db.clone(), lists(), 11).unwrap();
    for _ in 0..70 {
        let n = g.pending.as_ref().unwrap().options.len();
        g.step(rng.randrange(n)).unwrap();
    }
    let n = 20_000;
    let t = Instant::now();
    let mut keep = 0;
    for _ in 0..n {
        let c = g.clone();
        keep += c.history.len();
    }
    println!("clone at decision 70 (turn {}): {:.1} us  ({keep})", g.state.turn_number, t.elapsed().as_secs_f64() * 1e6 / n as f64);
    let t = Instant::now();
    for _ in 0..n {
        let c = g.fork();
        keep += c.history.len();
    }
    println!("fork (no log): {:.1} us", t.elapsed().as_secs_f64() * 1e6 / n as f64);
    let t = Instant::now();
    let mut ok = 0;
    for i in 0..n {
        let mut c = g.fork();
        ok += c.determinize(0, i as u64) as usize;
    }
    println!("fork + determinize: {:.1} us ({ok}/{n} fit)", t.elapsed().as_secs_f64() * 1e6 / n as f64);
}
