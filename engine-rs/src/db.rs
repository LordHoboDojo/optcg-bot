//! The card database: data/cards.json compiled into CardDefs (optcg/carddb.py).

use std::collections::HashMap;

use serde_json::Value;

use crate::cards::*;
use crate::compile::compile_card;
use crate::effects::{StaticNum, Statics};
use crate::scripts::script;

pub struct Db {
    pub defs: Vec<CardDef>,
    pub by_id: HashMap<String, u16>,
    /// The network's embedding index for each def (env.card_index()); 0 when unknown.
    pub emb: Vec<u32>,
}

impl Db {
    pub fn load(path: &str) -> Result<Db, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
        Db::from_json(&text)
    }

    pub fn from_json(text: &str) -> Result<Db, String> {
        let cards: Vec<Value> = serde_json::from_str(text).map_err(|e| e.to_string())?;
        let mut db = Db { defs: Vec::with_capacity(cards.len()), by_id: HashMap::new(), emb: vec![] };
        for j in &cards {
            let d = card_from_json(j)?;
            db.by_id.insert(d.id.clone(), db.defs.len() as u16);
            db.defs.push(d);
        }
        db.emb = vec![0; db.defs.len()];
        Ok(db)
    }

    /// Use the network's card numbering for observations.
    pub fn set_card_index(&mut self, index: &HashMap<String, u32>) {
        self.emb = self.defs.iter().map(|d| index.get(&d.id).copied().unwrap_or(0)).collect();
    }

    pub fn index(&self, id: &str) -> Result<u16, String> {
        self.by_id.get(id).copied().ok_or_else(|| format!("unknown card id {id}"))
    }

    pub fn get(&self, id: &str) -> Option<&CardDef> {
        self.by_id.get(id).map(|&i| &self.defs[i as usize])
    }
}

fn s(j: &Value, k: &str) -> String {
    j[k].as_str().unwrap_or("").to_string()
}

fn n(j: &Value, k: &str) -> i32 {
    j[k].as_i64().unwrap_or(0) as i32
}

fn strs(j: &Value, k: &str) -> Vec<String> {
    j[k].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default()
}

fn card_from_json(j: &Value) -> Result<CardDef, String> {
    let (effect, trigger) = (s(j, "effect"), s(j, "trigger"));
    let comp = compile_card(&effect, &trigger);
    let id = s(j, "id");
    let category = Category::parse(&s(j, "category")).ok_or_else(|| format!("{id}: bad category"))?;
    let mut d = CardDef {
        id,
        name: s(j, "name"),
        category,
        colors: strs(j, "colors"),
        cost: n(j, "cost"),
        power: n(j, "power"),
        counter: n(j, "counter"),
        life: n(j, "life"),
        types: strs(j, "types"),
        keywords: comp.keywords,
        effects: comp.effects,
        st: Statics {
            power: (!comp.static_power.is_empty()).then(|| StaticNum::Parts(comp.static_power)),
            cost: (!comp.static_cost.is_empty()).then(|| StaticNum::Parts(comp.static_cost)),
            keywords: comp.static_keywords,
            hand_cost: comp.hand_cost,
            ..Statics::default()
        },
        text: effect,
        trigger_text: trigger,
        unimplemented: comp.unimplemented,
    };
    if let Some(sc) = script(&d.id) {
        let added = (sc.apply)(&mut d);
        match sc.covers {
            None => {
                d.effects = added;
                d.unimplemented.clear();
            }
            Some(covers) => {
                d.effects.extend(added);
                d.unimplemented.retain(|u| !covers.iter().any(|c| u.contains(c)));
            }
        }
    }
    Ok(d)
}
