//! How much card text the engine implements: `cargo run --release --example coverage [-- --list]`.
use optcg_rs::db::Db;

fn main() {
    let db = Db::load("../data/cards.json").expect("card data");
    let list = std::env::args().any(|a| a == "--list");
    let (mut cards, mut lines) = (0, 0);
    for d in &db.defs {
        if !d.unimplemented.is_empty() {
            cards += 1;
            lines += d.unimplemented.len();
            if list {
                for u in &d.unimplemented {
                    println!("{}\t{}\t{}", d.id, u, optcg_rs::compile2::explain(u));
                }
            }
        }
    }
    eprintln!("{} cards, {} not fully implemented, {} lines missing", db.defs.len(), cards, lines);
}
