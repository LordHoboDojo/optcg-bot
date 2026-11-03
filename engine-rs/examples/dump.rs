//! What the second compiler read, line by line: `cargo run --release --example dump [-- ID_PREFIX]`.
use optcg_rs::compile::compile_card;
use optcg_rs::gen::{show, show_cond};

fn main() {
    let filter = std::env::args().nth(1).unwrap_or_default();
    let text = std::fs::read_to_string("../data/cards.json").unwrap();
    let cards: Vec<serde_json::Value> = serde_json::from_str(&text).unwrap();
    for j in cards {
        let id = j["id"].as_str().unwrap();
        if !id.starts_with(&filter) {
            continue;
        }
        let c = compile_card(j["effect"].as_str().unwrap_or(""), j["trigger"].as_str().unwrap_or(""));
        for line in &c.second {
            println!("{id}  {line}");
            for e in c.effects.iter().filter(|e| e.text == *line) {
                let cond = e.condition.as_ref().map(|c| format!(" if {}", show_cond(c))).unwrap_or_default();
                println!("    [{}]{cond} {}", e.timing.as_str(), show(&e.body));
            }
            let st = c.static_power.len() + c.static_keywords.len() + c.static_cost.len() + c.hand_cost.len() + c.gen_replacements.len();
            if c.effects.iter().all(|e| e.text != *line) && st > 0 {
                for (_, k, _) in &c.static_keywords { println!("    static keyword {k:?}"); }
                for (_, v, _) in &c.static_power { println!("    static power {v}"); }
                for (_, v, _) in &c.static_cost { println!("    static cost {v}"); }
                for (r, t, _) in &c.gen_replacements { println!("    replacement ko_only={} battle={:?} who={:?}: {t}", r.ko_only, r.battle, r.who.as_ref().map(optcg_rs::gen::show_pred)); }
            }
        }
    }
}
