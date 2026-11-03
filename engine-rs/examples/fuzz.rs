//! Random games with random decks of fully implemented cards, looking for panics and games that
//! never end: `cargo run --release --example fuzz -- [games] [seed]`.
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Arc;

use optcg_rs::cards::Category;
use optcg_rs::db::Db;
use optcg_rs::engine::{DeckList, Game};
use optcg_rs::rng::PyRandom;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let games: u64 = args.get(1).map_or(2000, |a| a.parse().unwrap());
    let seed: u64 = args.get(2).map_or(1, |a| a.parse().unwrap());
    let db = Arc::new(Db::load("../data/cards.json").unwrap());
    let ok: Vec<&optcg_rs::cards::CardDef> = db.defs.iter().filter(|d| d.unimplemented.is_empty()).collect();
    let leaders: Vec<_> = ok.iter().filter(|d| d.category == Category::Leader).collect();
    let mut rng = PyRandom::new(seed);
    let deck = |rng: &mut PyRandom| {
        let l = leaders[rng.randrange(leaders.len())];
        let pool: Vec<_> = ok
            .iter()
            .filter(|d| d.category != Category::Leader && d.colors.iter().any(|c| l.colors.contains(c)))
            .collect();
        let mut cards = vec![];
        while cards.len() < 50 {
            let d = pool[rng.randrange(pool.len())];
            for _ in 0..4.min(50 - cards.len()) {
                cards.push(d.id.clone());
            }
        }
        (l.id.clone(), cards)
    };
    let (mut panics, mut long) = (0, 0);
    for i in 0..games {
        let (a, b) = (deck(&mut rng), deck(&mut rng));
        let lists = [DeckList { leader: &a.0, cards: a.1.iter().map(|s| s.as_str()).collect() },
                     DeckList { leader: &b.0, cards: b.1.iter().map(|s| s.as_str()).collect() }];
        let mut g = Game::new(db.clone(), lists, i).unwrap();
        g.logging = true;
        let mut prng = PyRandom::new(i + 7);
        let r = catch_unwind(AssertUnwindSafe(|| {
            let mut steps = 0;
            while !g.over && steps < 6000 {
                let n = g.pending.as_ref().unwrap().options.len();
                g.step(prng.randrange(n)).unwrap();
                steps += 1;
            }
            steps
        }));
        match r {
            Err(_) => {
                panics += 1;
                eprintln!("game {i}: panic. decks {} vs {}; last log:", a.0, b.0);
                for l in g.log.iter().rev().take(6).rev() {
                    eprintln!("    {l}");
                }
            }
            Ok(6000) => {
                long += 1;
                eprintln!("game {i}: 6000 decisions without ending");
            }
            _ => {}
        }
    }
    println!("{games} games: {panics} panics, {long} too long");
}
