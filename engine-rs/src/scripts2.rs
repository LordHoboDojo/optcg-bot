//! Hand-written effects for cards neither compiler reads, in the Rust engine only (the Python
//! engine doesn't implement these cards). Same shape as scripts.rs.

use crate::cards::*;
use crate::effects::*;
use crate::engine::{Eng, R};
use crate::scripts::Script;

pub fn script2(id: &str) -> Option<Script> {
    let s = |covers, apply| Some(Script { covers, apply });
    match id {
        "OP06-033" => s(None, vander_decken),
        "OP08-043" => s(None, newgate_tax),
        "OP10-058" => s(None, rebecca),
        "OP12-020" => s(None, zoro_leader),
        "OP13-064" => s(Some(&["have their effects negated"]), roger),
        "OP15-024" => s(Some(&["cannot be rested by your opponent's Leader and Character effects"]), usopp),
        "OP15-093" => s(None, risky_brothers),
        "PRB02-006" => s(Some(&["would be rested by your opponent's Character's effect"]), zoro_swap),
        _ => None,
    }
}

fn effect(timing: Timing, run: BodyFn, text: &str) -> Effect {
    Effect::new(timing, Body::Script(run), text)
}

fn q(c: &str) -> Cond {
    Cond::Q(Box::new(crate::compile2::cond2(c).ok().expect("condition")))
}

/// [On Play] You may trash 1 {Fish-Man} type card from your hand or 1 [The Ark Noah] from your
/// hand or field: K.O. up to 1 of your opponent's rested Characters.
fn vander_decken(_: &mut CardDef) -> Vec<Effect> {
    fn options(e: &Eng, src: Uid) -> Vec<Uid> {
        let me = e.owner(src);
        let ps = e.ps(me);
        let mut v: Vec<Uid> = ps.hand.iter().copied().filter(|&c| e.d(c).has_type("Fish-Man") || e.d(c).named("The Ark Noah")).collect();
        v.extend(ps.field_iter().filter(|&c| c != src && e.d(c).named("The Ark Noah")));
        v
    }
    fn payable(e: &Eng, src: Uid) -> bool {
        !options(e, src).is_empty()
    }
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        let cands = options(e, src);
        if cands.is_empty() || !e.confirm(me, "Pay the cost of Vander Decken IX's effect?")? {
            return Ok(());
        }
        let c = e.choose_cards(me, cands, 1, "Trash for the cost", true)?[0];
        if e.c(c).zone == Some(Zone::Hand) {
            e.mv(c, Zone::Trash);
            e.hand_trashed(me, 1);
        } else {
            e.remove_from_field(c, Zone::Trash, Some(me), false, false)?;
        }
        let targets = e.characters(1 - me).into_iter().filter(|&t| e.c(t).rested).collect();
        for t in e.choose_cards(me, targets, 1, "K.O. a rested Character", false)? {
            e.ko(t, Some(me), true, false)?;
        }
        Ok(())
    }
    let mut eff = effect(Timing::OnPlay, run, "[On Play] You may trash 1 {Fish-Man} type card from your hand or 1 [The Ark Noah] from your hand or field: K.O. up to 1 of your opponent's rested Characters.");
    eff.condition = Some(Cond::Script(payable));
    vec![eff]
}

/// [On Play] If your Leader's type includes "Whitebeard Pirates" and you have 2 or less Life
/// cards, none of your opponent's Characters can attack until the end of their next turn unless
/// they trash 2 cards from their hand whenever they attack.
fn newgate_tax(_: &mut CardDef) -> Vec<Effect> {
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        for c in e.characters(1 - me) {
            e.grant_keyword(c, Kw::AttackTax, Until::NextEnd, Some(me));
        }
        Ok(())
    }
    let mut eff = effect(Timing::OnPlay, run, "[On Play] If your Leader's type includes \"Whitebeard Pirates\" and you have 2 or less Life cards, select all of your opponent's Characters on their field. Until the end of your opponent's next turn, none of the selected Characters can attack unless your opponent trashes 2 cards from their hand whenever they attack.");
    eff.body = Body::If(q("your Leader's type includes \"Whitebeard Pirates\" and you have 2 or less Life cards"), Box::new(eff.body));
    vec![eff]
}

/// [On Play] If there is a Character with a cost of 8 or more, draw 1 card. Then, reveal up to 2
/// {Dressrosa} type Character cards with a cost of 7 or less other than [Rebecca] from your hand.
/// Play 1 of the revealed cards and play the other card rested if it has a cost of 4 or less.
fn rebecca(_: &mut CardDef) -> Vec<Effect> {
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        if (0..2u8).flat_map(|p| e.characters(p)).any(|c| e.cost(c) >= 8) {
            e.effect_draw(me, 1, me);
        }
        let cands = e
            .ps(me)
            .hand
            .iter()
            .copied()
            .filter(|&c| {
                let d = e.d(c);
                d.category == Category::Character && d.has_type("Dressrosa") && e.cost(c) <= 7 && !d.named("Rebecca") && !d.st.no_effect_play
            })
            .collect();
        let shown = e.choose_cards(me, cands, 2, "Reveal from your hand", false)?;
        if shown.is_empty() {
            return Ok(());
        }
        let names: Vec<&str> = shown.iter().map(|&c| e.name(c)).collect();
        e.log(|| format!("Player {me} reveals {}", names.join(", ")));
        let first = e.choose_cards(me, shown.clone(), 1, "Play this one", true)?[0];
        e.play_card(first, false)?;
        if let Some(&other) = shown.iter().find(|&&c| c != first) {
            if e.cost(other) <= 4 {
                e.play_card(other, true)?;
            }
        }
        Ok(())
    }
    vec![effect(Timing::OnPlay, run, "[On Play] If there is a Character with a cost of 8 or more, draw 1 card. Then, reveal up to 2 {Dressrosa} type Character cards with a cost of 7 or less other than [Rebecca] from your hand. Play 1 of the revealed cards and play the other card rested if it has a cost of 4 or less.")]
}

/// [DON!! x3] [Activate: Main] [Once Per Turn] If this Leader battles your opponent's Character
/// during this turn, set this Leader as active. Then, this Leader cannot attack your opponent's
/// Characters with a base cost of 7 or less during this turn.
fn zoro_leader(_: &mut CardDef) -> Vec<Effect> {
    fn battled(e: &Eng, src: Uid) -> bool {
        e.ps(e.owner(src)).leader_battled_char_turn == e.s.turn_number
    }
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        if battled(e, src) {
            e.cm(src).rested = false;
            let me = e.owner(src);
            e.grant_keyword(src, Kw::NoAttackLowChars, Until::Turn, Some(me));
        }
        Ok(())
    }
    let mut eff = effect(Timing::ActivateMain, run, "[DON!! x3] [Activate: Main] [Once Per Turn] If this Leader battles your opponent's Character during this turn, set this Leader as active. Then, this Leader cannot attack your opponent's Characters with a base cost of 7 or less during this turn.");
    eff.don_req = 3;
    eff.once_per_turn = true;
    vec![eff]
}

/// Your Leader and all of your Characters that do not have a type including "Roger Pirates" have
/// their effects negated.
fn roger(d: &mut CardDef) -> Vec<Effect> {
    d.st.negate_own_except = Some("Roger Pirates".into());
    vec![]
}

/// [Opponent's Turn] This Character cannot be rested by your opponent's Leader and Character
/// effects and gains [Blocker].
fn usopp(d: &mut CardDef) -> Vec<Effect> {
    let cond = Cond::Spec { don_req: 0, your_turn: false, opp_turn: true, extra: None };
    d.st.keywords.push((cond.clone(), Kw::NoRestByOppCards, Target::SelfCard));
    d.st.keywords.push((cond, Kw::Blocker, Target::SelfCard));
    vec![]
}

/// [Activate: Main] You may trash this Character: If you have 15 or more cards in your trash, up
/// to 1 of your [Monkey.D.Luffy] Characters gains [Rush: Character] and the <Slash> attribute
/// during this turn.
fn risky_brothers(_: &mut CardDef) -> Vec<Effect> {
    fn on_field(e: &Eng, src: Uid) -> bool {
        e.c(src).zone == Some(Zone::Character)
    }
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        e.remove_from_field(src, Zone::Trash, Some(me), false, false)?;
        if e.ps(me).trash.len() < 15 {
            return Ok(());
        }
        let luffys = e.characters(me).into_iter().filter(|&c| e.d(c).named("Monkey.D.Luffy")).collect();
        for c in e.choose_cards(me, luffys, 1, "Gains [Rush: Character] and <Slash>", false)? {
            e.grant_keyword(c, Kw::RushCharacter, Until::Turn, Some(me));
            e.grant_keyword(c, Kw::AttrSlash, Until::Turn, Some(me));
        }
        Ok(())
    }
    let mut eff = effect(Timing::ActivateMain, run, "[Activate: Main] You may trash this Character: If you have 15 or more cards in your trash, up to 1 of your [Monkey.D.Luffy] Characters gains [Rush: Character] and the <Slash> attribute during this turn.");
    eff.condition = Some(Cond::Script(on_field));
    vec![eff]
}

/// [Opponent's Turn] If this Character would be rested by your opponent's Character's effect, you
/// may rest 1 of your other Characters instead.
fn zoro_swap(d: &mut CardDef) -> Vec<Effect> {
    d.st.rest_swap = true;
    vec![]
}
