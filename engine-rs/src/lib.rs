//! ONE PIECE CARD GAME rules engine in Rust: a port of the Python engine in optcg/, built as the
//! Python module `optcg_rs` (see engine-rs/README.md).

pub mod agents;
pub mod cards;
pub mod compile;
pub mod compile2;
pub mod db;
pub mod effects;
pub mod engine;
pub mod gen;
pub mod lethal;
pub mod rng;
pub mod scripts;
pub mod scripts2;

use serde_json::{json, Value};

use cards::*;
use engine::{Eng, Game};

/// Every keyword and marker, sorted by name (for snapshots).
const ALL_KW: [Kw; 11] = [
    Kw::Banish,
    Kw::CannotAttack,
    Kw::NoBlocker,
    Kw::NoKoByEffects,
    Kw::NoKoByOppEffects,
    Kw::NoKoInBattle,
    Kw::Blocker,
    Kw::DoubleAttack,
    Kw::Rush,
    Kw::RushCharacter,
    Kw::Unblockable,
];

fn field_card(e: &Eng, u: Uid) -> Value {
    let c = e.c(u);
    let mut kws: Vec<&str> = ALL_KW.iter().filter(|&&k| e.has_keyword(u, k)).map(|k| k.as_str()).collect();
    kws.sort();
    json!([u, e.d(u).id, e.power(u), e.cost(u), c.rested, c.don, kws])
}

/// The state as plain data, built the same way as tests/test_rust_engine.py builds it from
/// a Python game, so the two can be compared after every decision.
pub fn snapshot(e: &Eng) -> Value {
    let ids = |v: &[Uid]| v.iter().map(|&u| e.d(u).id.clone()).collect::<Vec<_>>();
    let players: Vec<Value> = (0..2u8)
        .map(|p| {
            let ps = e.ps(p);
            json!({
                "leader": field_card(e, ps.leader),
                "chars": ps.characters.iter().map(|&u| field_card(e, u)).collect::<Vec<_>>(),
                "stage": ps.stage.map(|u| field_card(e, u)),
                "hand": ps.hand.iter().map(|&u| json!([u, e.d(u).id, e.cost(u), e.counter_value(u), e.c(u).revealed])).collect::<Vec<_>>(),
                "deck": ids(&ps.deck),
                "trash": ids(&ps.trash),
                "life": ps.life.iter().map(|&u| json!([e.d(u).id, e.c(u).face_up])).collect::<Vec<_>>(),
                "don": [ps.don_deck, ps.don_active, ps.don_rested],
            })
        })
        .collect();
    json!({
        "turn": e.s.turn_number,
        "tp": e.s.turn_player,
        "phase": e.s.phase.as_str(),
        "battle": e.s.battle.as_ref().map(|b| json!([b.attacker, b.target])),
        "players": players,
    })
}

pub fn pending(g: &Game) -> Option<Value> {
    g.pending.as_ref().map(|d| {
        json!({
            "player": d.player,
            "kind": d.kind.as_str(),
            "prompt": d.prompt,
            "options": d.options.iter().map(|o| json!([o.kind.as_str(), o.card, o.target, o.index, o.text()])).collect::<Vec<_>>(),
        })
    })
}

#[cfg(feature = "python")]
mod py {
    use std::collections::HashMap;
    use std::sync::Arc;

    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;

    use crate::db::Db;
    use crate::engine::{DeckList, Game};

    /// The card database (data/cards.json), compiled once and shared by every game.
    #[pyclass(name = "Db", frozen, module = "optcg_rs")]
    pub struct PyDb {
        inner: Arc<Db>,
    }

    #[pymethods]
    impl PyDb {
        /// `card_index`: the network's card numbering (optcg.env.card_index()) for encode().
        #[new]
        #[pyo3(signature = (path, card_index=None))]
        fn new(path: &str, card_index: Option<HashMap<String, u32>>) -> PyResult<Self> {
            let mut db = Db::load(path).map_err(PyValueError::new_err)?;
            if let Some(ix) = card_index {
                db.set_card_index(&ix);
            }
            Ok(PyDb { inner: Arc::new(db) })
        }

        fn __len__(&self) -> usize {
            self.inner.by_id.len()
        }

        /// Effect lines of a card the engine does not carry out.
        fn unimplemented(&self, id: &str) -> PyResult<Vec<String>> {
            let d = self.inner.get(id).ok_or_else(|| PyValueError::new_err(format!("unknown card {id}")))?;
            Ok(d.unimplemented.clone())
        }

        fn effect_count(&self, id: &str) -> PyResult<usize> {
            let d = self.inner.get(id).ok_or_else(|| PyValueError::new_err(format!("unknown card {id}")))?;
            Ok(d.effects.len())
        }
    }

    /// A game. `decks` is [(leader id, [50 card ids in order]), ...] for players 0 and 1.
    #[pyclass(name = "Game", module = "optcg_rs")]
    pub struct PyGame {
        g: Game,
    }

    #[pymethods]
    impl PyGame {
        #[new]
        fn new(db: &PyDb, decks: Vec<(String, Vec<String>)>, seed: u64) -> PyResult<Self> {
            if decks.len() != 2 {
                return Err(PyValueError::new_err("need 2 decks"));
            }
            let lists = |i: usize| DeckList { leader: &decks[i].0, cards: decks[i].1.iter().map(|s| s.as_str()).collect() };
            Game::new(db.inner.clone(), [lists(0), lists(1)], seed).map(|g| PyGame { g }).map_err(PyValueError::new_err)
        }

        /// Answer the pending decision with option number `option`.
        fn step(&mut self, option: usize) -> PyResult<()> {
            self.g.step(option).map_err(PyValueError::new_err)
        }

        fn concede(&mut self, player: u8) -> PyResult<()> {
            self.g.concede(player).map_err(PyValueError::new_err)
        }

        /// An independent copy of the game, for search.
        fn clone(&self) -> PyGame {
            PyGame { g: self.g.clone() }
        }

        /// A fast copy for search: no log, and no log lines, labels or prompts from here on.
        fn fork(&self) -> PyGame {
            PyGame { g: self.g.fork() }
        }

        /// Reshuffle the cards `player` can't see (see Game::determinize). False if this
        /// shuffle didn't fit the current decision; the game is unchanged then.
        fn determinize(&mut self, player: u8, seed: u64) -> bool {
            self.g.determinize(player, seed)
        }

        #[getter]
        fn over(&self) -> bool {
            self.g.over
        }

        #[getter]
        fn winner(&self) -> Option<u8> {
            self.g.winner
        }

        #[getter]
        fn log(&self) -> Vec<String> {
            self.g.log.clone()
        }

        #[getter]
        fn log_len(&self) -> usize {
            self.g.log.len()
        }

        #[getter]
        fn turn_number(&self) -> i32 {
            self.g.state.turn_number
        }

        /// Log lines cost time; switch them off for search.
        #[setter]
        fn set_logging(&mut self, on: bool) {
            self.g.logging = on;
        }

        /// Option labels and prompts cost time; switch them off for search.
        #[setter]
        fn set_labels(&mut self, on: bool) {
            self.g.labels = on;
        }

        fn num_options(&self) -> usize {
            self.g.pending.as_ref().map_or(0, |d| d.options.len())
        }

        fn pending_player(&self) -> Option<u8> {
            self.g.pending.as_ref().map(|d| d.player)
        }

        /// Search-tree names for the pending options (Game::option_keys).
        fn option_keys(&self, player: u8) -> Vec<(u8, i64, i64, i64)> {
            self.g.option_keys(player)
        }

        fn pending_json(&self) -> Option<String> {
            crate::pending(&self.g).map(|v| v.to_string())
        }

        /// The network's observation for the pending decision (optcg.env.encode):
        /// (scalars, card rows, option rows, decision kind index, player).
        #[allow(clippy::type_complexity)]
        fn encode(&mut self) -> PyResult<(Vec<f64>, Vec<[f64; 16]>, Vec<[i64; 6]>, usize, u8)> {
            let d = self.g.pending.clone().ok_or_else(|| PyValueError::new_err("the game is over"))?;
            let o = self.g.view(|e| crate::agents::encode(e, &d));
            Ok((o.scalars, o.cards, o.options, o.decision, o.player))
        }

        /// The option GreedyAgent would pick.
        fn greedy(&mut self) -> PyResult<usize> {
            let d = self.g.pending.clone().ok_or_else(|| PyValueError::new_err("the game is over"))?;
            Ok(self.g.view(|e| crate::agents::greedy(e, &d)))
        }

        /// Whether `player` wins before this turn ends whatever the opponent does (lethal.rs):
        /// (True/False, or None past `budget` positions; positions searched).
        fn forced_win(&self, player: u8, budget: usize) -> (Option<bool>, usize) {
            crate::lethal::forced_win(&self.g, player, budget)
        }

        fn snapshot_json(&mut self) -> String {
            self.g.view(|e| crate::snapshot(e)).to_string()
        }
    }

    #[pymodule]
    fn optcg_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<PyDb>()?;
        m.add_class::<PyGame>()?;
        Ok(())
    }
}
