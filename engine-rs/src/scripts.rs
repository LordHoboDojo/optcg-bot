//! Hand-written effects for cards the compiler can't handle, ported from optcg/scripts.py.
//!
//! A script sets CardDef fields and returns effects. With `covers`, only the unimplemented lines
//! containing those strings are replaced and the effects are added to the compiled ones;
//! without it, the script's effects replace all of the card's effects.

use crate::cards::*;
use crate::effects::*;
use crate::engine::{Eng, R};

pub struct Script {
    pub covers: Option<&'static [&'static str]>,
    pub apply: fn(&mut CardDef) -> Vec<Effect>,
}

pub fn script(id: &str) -> Option<Script> {
    let s = |covers, apply| Some(Script { covers, apply });
    match id {
        "OP17-020" => s(None, shanks_leader),
        "OP17-039" => s(None, xebec_leader),
        "ST20-003" => s(Some(&["Look at up to 1 card from the top of your or your opponent's Life"]), brulee),
        "OP17-015" => s(Some(&["would be removed"]), marco),
        "OP17-021" => s(Some(&["would be removed"]), crone_oli),
        "OP17-023" => s(Some(&["would be K.O.'d"]), nami),
        "OP17-043" => s(Some(&["would be removed"]), ganzui),
        "OP17-045" => s(Some(&["would be removed"]), kyo),
        "OP17-095" => s(Some(&["would be removed"]), zoro),
        "OP17-007" => s(Some(&["[On Play]"]), oden),
        "OP17-040" => s(Some(&["attacks or is attacked"]), newgate_rocks),
        "OP17-044" => s(Some(&["cannot attack any card"]), captain_john),
        "OP17-062" => s(None, kaido_don),
        "OP17-063" => s(None, kaido_negate),
        "OP17-112" => s(Some(&["The base power"]), linlin_base_power),
        "OP17-118" => s(None, xebec_character),
        "OP17-119" => s(Some(&["+12 cost"]), loki),
        "EB05-010" => s(Some(&["When one of your Characters"]), robin_leader),
        "EB05-052" => s(Some(&["If you would take damage"]), gloriosa),
        "EB05-061" => s(Some(&["would be removed from the field by your opponent"]), nami_life),
        _ => None,
    }
}

fn leader(e: &Eng, src: Uid) -> Uid {
    e.ps(e.owner(src)).leader
}

fn effect(timing: Timing, run: BodyFn, text: &str) -> Effect {
    Effect::new(timing, Body::Script(run), text)
}

// ---------------------------------------------------------------- OP-17 Leaders

/// [Activate: Main] [Once Per Turn] Trash 1 card or rest 1 DON!!: up to 1 of your opponent's
/// rested Characters won't become active in their next Refresh Phase.
fn shanks_leader(_: &mut CardDef) -> Vec<Effect> {
    fn payable(e: &Eng, src: Uid) -> bool {
        let ps = e.ps(e.owner(src));
        !ps.hand.is_empty() || ps.don_active > 0
    }
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        let mut opts = vec![];
        if !e.ps(me).hand.is_empty() {
            opts.push(Action::new(ActKind::Select).index(0).label("Trash 1 card from your hand"));
        }
        if e.ps(me).don_active > 0 {
            opts.push(Action::new(ActKind::Select).index(1).label("Rest 1 of your DON!! cards"));
        }
        let a = e.choose(me, DecisionKind::Cost, opts, "Pay Shanks's cost")?;
        if a.index == Some(0) {
            e.trash_from_hand(me, 1)?;
        } else {
            let ps = e.psm(me);
            ps.don_active -= 1;
            ps.don_rested += 1;
        }
        let targets = e.characters(1 - me).into_iter().filter(|&c| e.c(c).rested).collect();
        for c in e.choose_cards(me, targets, 1, "Won't become active next Refresh Phase", false)? {
            e.skip_next_refresh(c, me);
        }
        Ok(())
    }
    let mut eff = effect(Timing::ActivateMain, run, "Keep an opponent's rested Character rested");
    eff.once_per_turn = true;
    eff.condition = Some(Cond::Script(payable));
    vec![eff]
}

/// [When Attacking] You may trash 1 card: reveal the top card of your deck; if its type
/// includes "Rocks Pirates", draw 2 cards.
fn xebec_leader(_: &mut CardDef) -> Vec<Effect> {
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        if !e.confirm(me, "Trash 1 card to reveal the top card of your deck?")? {
            return Ok(());
        }
        e.trash_from_hand(me, 1)?;
        let Some(&top) = e.ps(me).deck.last() else { return Ok(()) };
        let name = e.name(top);
        e.log(|| format!("Player {me} reveals {name}"));
        if e.d(top).type_includes("Rocks Pirates") {
            e.draw(me, 2);
        }
        Ok(())
    }
    let mut eff = effect(Timing::WhenAttacking, run, "Trash 1: reveal the top card; draw 2 if it's Rocks Pirates");
    eff.condition = Some(Cond::Script(|e, src| !e.ps(e.owner(src)).hand.is_empty()));
    vec![eff]
}

// ---------------------------------------------------------------- other sets

/// [Trigger] Look at the top card of your or your opponent's Life, put it on the top or bottom
/// of those Life cards, then add this card to your hand.
fn brulee(_: &mut CardDef) -> Vec<Effect> {
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        let mut opts = vec![Action::new(ActKind::Pass).label("Don't look")];
        for (p, whose) in [(me, "your"), (1 - me, "your opponent's")] {
            if !e.ps(p).life.is_empty() {
                opts.push(Action::new(ActKind::Select).index(p as i32).label(format!("Look at the top of {whose} Life cards")));
            }
        }
        let a = e.choose(me, DecisionKind::Select, opts, "Charlotte Brulee: look at a Life card?")?;
        if a.kind == ActKind::Select {
            let p = a.index.unwrap() as u8;
            let top = *e.ps(p).life.last().unwrap();
            if e.confirm(me, &format!("The top card is {}. Move it to the bottom?", e.name(top)))? {
                let life = &mut e.psm(p).life;
                let c = life.pop().unwrap();
                life.insert(0, c);
            }
        }
        e.mv(src, Zone::Hand); // instead of the usual trash after a [Trigger] (10-1-5-3)
        Ok(())
    }
    vec![effect(Timing::Trigger, run, "Look at a top Life card, then add this card to your hand")]
}

// ---------------------------------------------------------------- replacement effects

/// Applies when one of your Characters would be removed from the field by your opponent's effect.
fn by_opponent(e: &Eng, src: Uid, card: Uid, by: Option<u8>) -> bool {
    e.owner(card) == e.owner(src) && e.c(card).zone == Some(Zone::Character) && by == Some(1 - e.owner(src))
}

fn trash_two(e: &mut Eng, src: Uid, _card: Uid) -> R<()> {
    e.trash_from_hand(e.owner(src), 2).map(|_| ())
}

fn hand_at_least_two(e: &Eng, src: Uid) -> bool {
    e.ps(e.owner(src)).hand.len() >= 2
}

fn replacement(d: &mut CardDef, r: Replacement) -> Vec<Effect> {
    d.st.replacements = vec![r];
    vec![]
}

fn marco(d: &mut CardDef) -> Vec<Effect> {
    replacement(
        d,
        Replacement {
            applies: |e, src, card, by, _| by_opponent(e, src, card, by),
            pay: |e, src, _| e.ko(src, Some(e.owner(src)), false, false),
            payable: |e, src| e.c(src).zone == Some(Zone::Character),
            text: "K.O. Marco".into(),
            once_per_turn: false,
        },
    )
}

fn crone_oli(d: &mut CardDef) -> Vec<Effect> {
    replacement(
        d,
        Replacement {
            applies: |e, src, card, by, _| by_opponent(e, src, card, by) && e.d(card).type_includes("Red-Haired Pirates"),
            pay: |e, src, _| e.rest_your_cards(e.owner(src), 1),
            payable: |e, src| e.active_card_count(e.owner(src)) >= 1,
            text: "Rest 1 of your cards".into(),
            once_per_turn: false,
        },
    )
}

fn nami(d: &mut CardDef) -> Vec<Effect> {
    replacement(
        d,
        Replacement {
            applies: |e, src, card, _, ko| {
                ko && e.owner(card) == e.owner(src)
                    && e.c(card).zone == Some(Zone::Character)
                    && (e.d(card).has_type("East Blue") || e.d(card).has_type("Straw Hat Crew"))
            },
            pay: |e, src, _| {
                e.cm(src).rested = true;
                Ok(())
            },
            payable: |e, src| e.c(src).zone == Some(Zone::Character) && !e.c(src).rested,
            text: "Rest Nami".into(),
            once_per_turn: false,
        },
    )
}

fn ganzui(d: &mut CardDef) -> Vec<Effect> {
    replacement(
        d,
        Replacement {
            applies: |_, src, card, _, _| card == src,
            pay: trash_two,
            payable: hand_at_least_two,
            text: "Trash 2 cards from your hand".into(),
            once_per_turn: false,
        },
    )
}

fn kyo(d: &mut CardDef) -> Vec<Effect> {
    replacement(
        d,
        Replacement {
            applies: |e, src, card, by, _| by_opponent(e, src, card, by),
            pay: trash_two,
            payable: hand_at_least_two,
            text: "Trash 2 cards from your hand".into(),
            once_per_turn: false,
        },
    )
}

fn zoro(d: &mut CardDef) -> Vec<Effect> {
    replacement(
        d,
        Replacement {
            applies: |e, src, card, by, _| by_opponent(e, src, card, by),
            pay: |e, src, _| {
                let me = e.owner(src);
                let trash = e.ps(me).trash.to_vec();
                let chosen = e.choose_cards(me, trash, 3, "Place on the bottom of your deck", true)?;
                e.place_in_order(me, chosen, Zone::Deck, true)
            },
            payable: |e, src| e.ps(e.owner(src)).trash.len() >= 3,
            text: "Place 3 cards from your trash at the bottom of your deck".into(),
            once_per_turn: false,
        },
    )
}

// ---------------------------------------------------------------- other OP-17 cards

/// Plays a {Land of Wano} or "Whitebeard Pirates" Character with 6000 power or less.
fn oden(_: &mut CardDef) -> Vec<Effect> {
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let l = e.d(leader(e, src));
        if l.name != "Edward.Newgate" && !l.has_type("Land of Wano") {
            return Ok(());
        }
        let me = e.owner(src);
        let cands = e
            .ps(me)
            .hand
            .iter()
            .copied()
            .filter(|&c| {
                let d = e.d(c);
                d.category == Category::Character
                    && d.power <= 6000
                    && (d.has_type("Land of Wano") || d.type_includes("Whitebeard Pirates"))
            })
            .collect();
        for c in e.choose_cards(me, cands, 1, "Play a Character", false)? {
            e.play_card(c, false)?;
        }
        Ok(())
    }
    vec![effect(Timing::OnPlay, run, "Play a Wano or Whitebeard Character with 6000 power or less")]
}

fn newgate_rocks(_: &mut CardDef) -> Vec<Effect> {
    fn cond(e: &Eng, src: Uid) -> bool {
        let l = leader(e, src);
        match &e.s.battle {
            Some(b) => {
                (l == b.attacker || l == b.target)
                    && e.d(l).type_includes("Rocks Pirates")
                    && !e.ps(e.owner(src)).hand.is_empty()
            }
            None => false,
        }
    }
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        if !e.confirm(me, "Trash 1 card: your Leader gains +3000 power this battle?")? {
            return Ok(());
        }
        e.trash_from_hand(me, 1)?;
        let l = leader(e, src);
        e.add_power(l, 3000, Until::Battle, Some(me));
        Ok(())
    }
    let mut eff = effect(Timing::AttackDeclared, run, "Trash 1: your Leader gains +3000 power this battle");
    eff.once_per_turn = true;
    eff.condition = Some(Cond::Script(cond));
    vec![eff]
}

fn captain_john(d: &mut CardDef) -> Vec<Effect> {
    d.st.attack_lock = Some(|e, src| e.c(src).rested && e.d(leader(e, src)).type_includes("Rocks Pirates"));
    vec![]
}

fn kaido_don(_: &mut CardDef) -> Vec<Effect> {
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        e.add_don(me, 1, false);
        let ps = e.psm(me);
        if ps.don_rested > 0 {
            ps.don_rested -= 1;
            ps.don_active += 1;
        }
        Ok(())
    }
    let mut eff = effect(Timing::DonReturned, run, "Add 1 active DON!!, then set 1 DON!! as active");
    eff.your_turn = true;
    eff.once_per_turn = true;
    vec![eff]
}

fn kaido_negate(d: &mut CardDef) -> Vec<Effect> {
    d.st.counter = Some(|e, src, card| {
        let dc = e.d(card);
        if e.owner(card) == e.owner(src)
            && e.c(card).zone == Some(Zone::Hand)
            && dc.category == Category::Character
            && dc.counter == 0
        {
            Some(1000)
        } else {
            None
        }
    });
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        if e.c(src).played_turn != e.s.turn_number {
            return Ok(());
        }
        let me = e.owner(src);
        let cands = e.characters(1 - me).into_iter().filter(|&c| e.cost(c) <= 6).collect();
        for c in e.choose_cards(me, cands, 1, "Negate and K.O.", false)? {
            e.negate(c, Until::Turn, Some(me));
            e.ko(c, Some(me), true, false)?;
        }
        Ok(())
    }
    let mut eff = effect(Timing::ActivateMain, run, "If played this turn: negate and K.O. a cost 6 or less");
    eff.once_per_turn = true;
    eff.don_minus = 1;
    vec![eff]
}

fn linlin_base_power(d: &mut CardDef) -> Vec<Effect> {
    d.st.base_power = Some(|e, src, card| {
        let dc = e.d(card);
        let me = e.owner(src);
        if e.s.turn_player == me
            && e.owner(card) == me
            && e.c(card).zone == Some(Zone::Character)
            && !dc.trigger_text.is_empty()
            && dc.power == 4000
        {
            Some(8000)
        } else {
            None
        }
    });
    vec![]
}

fn xebec_character(d: &mut CardDef) -> Vec<Effect> {
    d.st.hand_counter =
        Some(|e, card| if e.characters(e.owner(card)).into_iter().all(|c| e.d(c).counter == 0) { Some(2000) } else { None });
    fn run(e: &mut Eng, src: Uid) -> R<()> {
        let me = e.owner(src);
        e.draw(me, 1);
        let mut left = 9;
        let mut names: Vec<&str> = vec![];
        for _ in 0..2 {
            let cands = e
                .ps(me)
                .hand
                .iter()
                .copied()
                .filter(|&c| {
                    let d = e.d(c);
                    matches!(d.category, Category::Character | Category::Stage)
                        && d.has_type("Rocks Pirates")
                        && e.cost(c) <= left
                        && !names.contains(&d.name.as_str())
                })
                .collect();
            let pick = e.choose_cards(me, cands, 1, &format!("Play a Rocks Pirates card (cost {left} or less)"), false)?;
            let Some(&p) = pick.first() else { return Ok(()) };
            left -= e.cost(p);
            names.push(e.name(p));
            e.play_card(p, false)?;
        }
        Ok(())
    }
    vec![effect(Timing::OnPlay, run, "Draw 1, then play up to 2 Rocks Pirates cards, total cost 9")]
}

fn loki(d: &mut CardDef) -> Vec<Effect> {
    d.st.cost = Some(StaticNum::Script(|_, src, card| if card == src { 12 } else { 0 }));
    d.st.power =
        Some(StaticNum::Script(|e, src, card| if card == src && e.s.turn_player != e.owner(src) { 3000 } else { 0 }));
    vec![]
}

// ---------------------------------------------------------------- EB-05

/// [Once Per Turn] When one of your Characters with the <Wisdom> attribute and without [Blocker]
/// is K.O.'d, add up to 1 card from the top of your deck to the top of your Life cards.
fn robin_leader(_: &mut CardDef) -> Vec<Effect> {
    let mut eff = Effect::new(
        Timing::AllyKo,
        Body::T(Tpl::DeckToLife { n: 1 }),
        "When a <Wisdom> Character without [Blocker] is K.O.'d: top of deck to Life",
    );
    eff.once_per_turn = true;
    eff.ko_filter = Some(|e, _src, card| {
        let d = e.d(card);
        d.category == Category::Character && d.attributes.iter().any(|a| a == "Wisdom") && !e.has_keyword(card, Kw::Blocker)
    });
    vec![eff]
}

/// If you would take damage, you may trash this Character instead (offered for each point of
/// damage, including the one that would defeat you at 0 Life).
fn gloriosa(d: &mut CardDef) -> Vec<Effect> {
    d.st.damage_replacements = vec![DamageReplacement {
        applies: |e, src, player| e.owner(src) == player,
        pay: |e, src| {
            e.mv(src, Zone::Trash);
            Ok(())
        },
        payable: |e, src| e.c(src).zone == Some(Zone::Character),
        text: "Trash Gloriosa".into(),
    }];
    vec![]
}

/// [Once Per Turn] If one of your Characters with 6000 base power or less would be removed from
/// the field by your opponent (their effects, or a K.O. in battle), you may add 1 card from the top
/// of your Life cards to your hand instead.
fn nami_life(d: &mut CardDef) -> Vec<Effect> {
    replacement(
        d,
        Replacement {
            applies: |e, src, card, by, ko| {
                e.owner(card) == e.owner(src)
                    && e.c(card).zone == Some(Zone::Character)
                    && e.base_power(card) <= 6000
                    && (by == Some(1 - e.owner(src)) || (ko && by.is_none()))
            },
            pay: |e, src, _| {
                let top = *e.ps(e.owner(src)).life.last().unwrap();
                e.mv(top, Zone::Hand);
                Ok(())
            },
            payable: |e, src| !e.ps(e.owner(src)).life.is_empty(),
            text: "Add the top Life card to your hand".into(),
            once_per_turn: true,
        },
    )
}
