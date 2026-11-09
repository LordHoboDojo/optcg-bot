//! Does a player have lethal? An exhaustive search of the rest of the current turn for a win
//! that no answer of the opponent's prevents (tools/lethal.py measures how often bots take it).
//!
//! Every choice of the player's is tried and every choice of the opponent's (blocks, counters,
//! Triggers...) must fail them. The search sees the whole game, the opponent's hand and the
//! deck order included. Positions reached by different orders of the same moves (DON!! to A
//! then B, or B then A) are searched once: a position is named by the snapshot, the decision and
//! the set of moves made this turn, so once-per-turn uses the snapshot doesn't show still count.
use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap;
use std::hash::{Hash, Hasher};

use crate::cards::ActKind;
use crate::engine::Game;

/// Moves after which a line is given up as not a win (no turn is this long).
const MAX_DEPTH: usize = 400;

/// Whether `me` wins before the current turn ends whatever the opponent does: Some(answer), or
/// None when the search passes `budget` positions; and the positions searched.
pub fn forced_win(g: &Game, me: u8, budget: usize) -> (Option<bool>, usize) {
    let mut s = Search { me, turn: g.state.turn_number, budget, nodes: 0, memo: HashMap::new() };
    let r = s.win(&mut g.fork(), &mut Vec::new());
    (r, s.nodes)
}

type Move = (u8, u8, i64, i64, i64);

struct Search {
    me: u8,
    turn: i32,
    budget: usize,
    nodes: usize,
    memo: HashMap<u64, bool>,
}

impl Search {
    fn win(&mut self, g: &mut Game, done: &mut Vec<Move>) -> Option<bool> {
        if g.over {
            return Some(g.winner == Some(self.me));
        }
        let Some(player) = g.pending.as_ref().map(|d| d.player) else { return Some(false) };
        if g.state.turn_number != self.turn || done.len() > MAX_DEPTH {
            return Some(false);
        }
        let key = {
            let mut h = DefaultHasher::new();
            g.view(crate::snapshot).to_string().hash(&mut h);
            crate::pending(g).map(|v| v.to_string()).hash(&mut h);
            let mut moves = done.clone();
            moves.sort_unstable();
            moves.hash(&mut h);
            h.finish()
        };
        if let Some(&r) = self.memo.get(&key) {
            return Some(r);
        }
        self.nodes += 1;
        if self.nodes > self.budget {
            return None;
        }
        let mine = player == self.me;
        let leader = g.state.players[1 - self.me as usize].leader;
        let opts: Vec<(ActKind, Option<u32>)> =
            g.pending.as_ref().unwrap().options.iter().map(|o| (o.kind, o.target.map(|t| t as u32))).collect();
        let keys = g.option_keys(player);
        let mut order: Vec<usize> = (0..keys.len()).collect();
        if mine {
            // attacks on the Leader first, they're how a turn is won; ending the turn never wins it
            order.retain(|&i| opts[i].0 != ActKind::End);
            order.sort_by_key(|&i| match opts[i] {
                (ActKind::Attack, Some(t)) if t == leader as u32 => 0,
                (ActKind::Attack, _) => 1,
                _ => 2,
            });
        } else {
            // the answers most likely to stop it first: blocks and counters before passing
            order.sort_by_key(|&i| matches!(opts[i].0, ActKind::Pass | ActKind::No));
        }
        let mut result = !mine; // mine: some option wins; theirs: every option must fail them
        for i in order {
            let mut c = g.clone();
            if c.step(i).is_err() {
                // not a real option here; it can't win or stop a win
                continue;
            }
            let k = keys[i];
            done.push((player, k.0, k.1, k.2, k.3));
            let w = self.win(&mut c, done);
            done.pop();
            if w? == mine {
                result = mine;
                break;
            }
        }
        self.memo.insert(key, result);
        Some(result)
    }
}
