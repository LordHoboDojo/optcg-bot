//! The second compiler: card text the first compiler (compile.rs, kept in step with Python)
//! can't read, compiled into general effects (gen.rs).
//!
//! It reads a sentence as its parts: an optional condition ("If you have 2 or less Life
//! cards, "), an action ("K.O.", "Give ... -2000 power", "... gains [Rush]"), the cards it acts
//! on ("up to 1 of your opponent's rested Characters with a cost of 4 or less") and how long it
//! lasts ("during this turn"). A line is only implemented if every part of it is read; anything
//! else stays in CardDef::unimplemented. Where a sentence reads as one of the first compiler's
//! templates, that template is used.

use crate::cards::*;
use crate::compile::{
    cap, cond_part, end, g, gi, gu, parse_body, parse_costs, rfull, rmatch, Compiled, Res, Spec, Unsupported,
    CHOOSE_ONE,
};
use crate::effects::*;
use crate::engine::LaterVerb;
use crate::gen::*;

const COLORS: &str = "red|green|blue|purple|black|yellow";

// ---------------------------------------------------------------- lines

/// Card texts in the data that run two effects together or leave a word out, as printed.
fn correction(raw: &str) -> Option<Vec<&'static str>> {
    let raw = raw.trim();
    Some(match raw {
        _ if raw.starts_with("[On Play] Give up to 1 rested DON!! card to your Leader or 1 of your Characters.[On Your Opponent's Attack]") => vec![
            "[On Play] Give up to 1 rested DON!! card to your Leader or 1 of your Characters.",
            "[On Your Opponent's Attack] [Once Per Turn] You may trash 1 card from your hand: If your Leader has the {Land of Wano} or {Whitebeard Pirates} type, give up to 1 of your opponent's Leader or Character cards -2000 power during this turn.",
        ],
        "[Counter] Up to 1 of your Leader or Character cards gains +2000 power during this battle. Then, if you have 2 or less Life cards, that card gains an additional +2000 power." => vec![
            "[Counter] Up to 1 of your Leader or Character cards gains +2000 power during this battle. Then, if you have 2 or less Life cards, that card gains an additional +2000 power during this battle.",
        ],
        _ if raw.starts_with("[On K.O.] Add up to 1 DON!! card from your DON!! deck and rest it, look at 5 cards") => vec![
            "[On K.O.] Add up to 1 DON!! card from your DON!! deck and rest it. Then, look at 5 cards from the top of your deck; reveal up to 1 {Donquixote Pirates} type card and add it to your hand. Then, place the rest at the bottom of your deck in any order.",
        ],
        _ if raw.starts_with("[DON!! x1] When you activate an Event, you may draw 1 card if you have 4 or less cards in your hand and haven't drawn") => vec![
            "[DON!! x1] [Once Per Turn] When you activate an Event, if you have 4 or less cards in your hand, you may draw 1 card.",
        ],
        _ => return None,
    })
}

pub fn compile_line2(line: &str, raw: &str, out: &mut Compiled, bullets: &[String]) -> Res<()> {
    if raw.contains("  attribute") {
        // The attribute name is missing from these card texts in the data (OP03-032, OP04-042,
        // OP06-026, ST32-001/003/004/005: all swordsman cards, themselves <Slash>). Read as <Slash>.
        let fixed = raw.replace("  attribute", " <Slash> attribute");
        return compile_line2_inner(&normalize2(&fixed), raw, out, bullets);
    }
    if let Some(lines) = correction(raw) {
        for l in lines {
            compile_line2_inner(&normalize2(l), l, out, bullets)?;
        }
        return Ok(());
    }
    compile_line2_inner(line, raw, out, bullets)
}

fn compile_line2_inner(line: &str, raw: &str, out: &mut Compiled, bullets: &[String]) -> Res<()> {
    let mut tags: Vec<String> = vec![];
    let mut alternatives = false;
    let mut line = line.to_string();
    while let Some(m) = rmatch(r"(/)?\[([^\]]+)\]\s*", &line) {
        alternatives |= m.get(1).is_some();
        tags.push(m[2].to_string());
        line = line[end(&m)..].to_string();
    }
    let body = line;
    let tags: Vec<String> = tags.into_iter().filter(|t| Kw::printed(t).is_none()).collect();
    let mut timings = vec![];
    let mut spec = Spec::default();
    for t in &tags {
        if let Some(tm) = Timing::from_tag(t) {
            timings.push(tm);
        } else if t == "Once Per Turn" {
            spec.once_per_turn = true;
        } else if let Some(m) = rfull(r"DON!! x(\d+)", t) {
            spec.don_req = m[1].parse().unwrap();
        } else if t == "Your Turn" {
            spec.your_turn = true;
        } else if t == "Opponent's Turn" {
            spec.opp_turn = true;
        } else {
            return Err(Unsupported);
        }
    }
    if timings.len() > 1 && !alternatives {
        return Err(Unsupported);
    }
    if timings.is_empty() {
        if let Some(m) = rfull(r"Apply each of the following effects based on the number of cards in your trash:", &body) {
            let _ = m;
            if bullets.is_empty() {
                return Err(Unsupported);
            }
            for b in bullets {
                let x = rfull(r"If (?:there are|you have) (?P<n>\d+) or more cards, (?P<rest>.+)", b).ok_or(Unsupported)?;
                static2(&format!("If you have {} or more cards in your trash, {}", &x["n"], &x["rest"]), &spec, out)?;
            }
            return Ok(());
        }
        if !bullets.is_empty() {
            return Err(Unsupported);
        }
        if let Some(rest) = body.strip_prefix("Then, ") {
            // a line that goes on with the effect before it
            let more = body2(&cap(rest))?;
            let e = out.effects.last_mut().ok_or(Unsupported)?;
            e.body = Body::Seq(Box::new(e.body.clone()), Box::new(more));
            return Ok(());
        }
        if let Some(m) = rmatch(r"^If (?P<ev>a Character is rested by your effect|this Character battles your opponent's Character), (?P<rest>.+)$", &body) {
            let ev = if m["ev"].starts_with("a Character") {
                (Timing::RestedByEffect, None)
            } else {
                (Timing::AttackDeclared, Some(Q::BattlesChar))
            };
            push_events(out, vec![ev], body2(&cap(&m["rest"]))?, raw, &spec, false);
            return Ok(());
        }
        if let Some(m) = rmatch(r"^At the end of a battle in which this Character battles your opponent's Character(?P<q> with [^,]+)?, (?P<rest>.+)$", &body) {
            let mut pred = Pred { cats: vec![Category::Character], ..Pred::default() };
            if let Some(q) = g(&m, "q") {
                let mut r = q.to_string();
                while !r.is_empty() {
                    r = qualifier(&r, &mut pred)?;
                }
            }
            let action = Body::Seq(Box::new(Body::Do(Do::ItIsBattleOpponent)), Box::new(body2(&cap(&m["rest"]))?));
            push_events(out, vec![(Timing::EndOfBattle, Some(Q::BattlesWith(pred)))], action, raw, &spec, false);
            return Ok(());
        }
        if let Some(m) = rmatch(r"^(?P<c>[^:]+): When (?P<ev>.+?), (?P<rest>.+)$", &body) {
            // "[Opponent's Turn] You may trash 1 card from your hand: When this Character is K.O.'d ..."
            if let (Ok(costs), Some(list)) = (costs2(&m["c"]), events(&m["ev"])) {
                let (action, cond) = costs.wrap(&body2(&cap(&m["rest"]))?, true);
                let group = if list.len() > 1 { Some(out.effects.len()) } else { None };
                for (t, q) in list {
                    let mut e = Effect::new(t, action.clone(), raw);
                    e.once_per_turn = spec.once_per_turn;
                    e.don_req = spec.don_req;
                    e.your_turn = spec.your_turn;
                    e.opp_turn = spec.opp_turn;
                    e.rest_don = costs.engine.0;
                    e.don_minus = costs.engine.1;
                    e.rest_self = costs.engine.2;
                    e.optional = costs.engine();
                    let mut conds: Vec<Cond> = cond.clone().into_iter().collect();
                    conds.extend(q.map(|q| Cond::Q(Box::new(q))));
                    e.condition = match conds.len() {
                        0 => None,
                        1 => conds.pop(),
                        _ => Some(Cond::All(conds)),
                    };
                    e.opt_group = group;
                    out.effects.push(e);
                }
                return Ok(());
            }
        }
        if let Some(m) = rmatch(r"^This effect can be activated (?:when|at) (?P<ev>.+?)\. (?P<rest>.+)$", &body) {
            let ev = &m["ev"];
            let (list, attacker) = match ev {
                "your opponent attacks" => (vec![(Timing::OnOppAttack, None)], true),
                "your opponent's Character attacks" => (
                    vec![(Timing::OnOppAttack, Some(Q::AttackerIs(Pred { cats: vec![Category::Character], ..Pred::default() })))],
                    true,
                ),
                "the start of your turn" => (vec![(Timing::StartOfTurn, Some(Q::YourTurn))], false),
                _ => (events(ev).ok_or(Unsupported)?, false),
            };
            let mut action = body2(&cap(&m["rest"]))?;
            if attacker {
                // "If that Character has the <Slash> attribute": the attacker
                action = Body::Seq(Box::new(Body::Do(Do::ItIsAttacker)), Box::new(action));
            }
            push_events(out, list, action, raw, &spec, true);
            return Ok(());
        }
        if let Some(m) = rmatch(r"^(?P<rest>[^,]+?) when (?P<ev>[^,]+?)\.$", &body) {
            if let Some(list) = events(&m["ev"]) {
                let action = body2(&format!("{}.", &m["rest"]))?;
                push_events(out, list, action, raw, &spec, false);
                return Ok(());
            }
        }
        if let Some(m) = rmatch(r"^When (?P<ev>.+?), (?P<rest>.+)$", &body) {
            if let Some(list) = events(&m["ev"]) {
                let action = body2(&cap(&m["rest"]))?;
                push_events(out, list, action, raw, &spec, false);
                return Ok(());
            }
        }
        return static2(&body, &spec, out);
    }

    // costs: the text before the first colon, if it reads as costs
    let mut costs = CostParts::default();
    let mut body = body;
    let mut pre_q: Option<Q> = None;
    if let Some(m) = rmatch(r"^(.*?)\s*:\s*(.*)$", &body) {
        if let Ok(c) = costs2(&m[1]) {
            costs = c;
            body = m[2].to_string();
        } else if let Some((q, rest)) = lead_condition(&m[1]) {
            // "If your Leader is [Imu], you may rest ...: ..." — the condition is checked to activate
            if let Ok(c) = costs2(&cap(&rest)) {
                costs = c;
                pre_q = Some(q);
                body = m[2].to_string();
            }
        }
    }
    if let Some(m) = rmatch(r"^This effect can be activated when (?P<c>[^.]+)\. (?P<rest>.+)$", &body) {
        if let Ok(q) = cond2(&m["c"]) {
            pre_q = Some(match pre_q {
                Some(p) => Q::And(vec![p, q]),
                None => q,
            });
            body = m["rest"].to_string();
        }
    }

    let action = if let Some((q, rest)) = lead_condition(&body).filter(|(_, r)| r == "choose one:") {
        let _ = rest;
        let mut options = vec![];
        for b in bullets {
            options.push((b.clone(), body2(&cap(b))?));
        }
        Body::If(Cond::Q(Box::new(q)), Box::new(Body::ChooseOne { opponent: false, options }))
    } else if let Some(cm) = rfull(CHOOSE_ONE, &body) {
        if bullets.is_empty() {
            return Err(Unsupported);
        }
        let mut options = vec![];
        for b in bullets {
            options.push((b.clone(), body2(&cap(b))?));
        }
        let mut action = Body::ChooseOne { opponent: cm["who"].starts_with("Your opponent"), options };
        if let Some(pre) = g(&cm, "pre").or_else(|| g(&cm, "pre2")) {
            let first = body2(&(cap(pre.trim_end_matches('.')) + "."))?;
            action = Body::Seq(Box::new(first), Box::new(action));
        }
        action
    } else if !bullets.is_empty() {
        return Err(Unsupported);
    } else {
        body2(&body)?
    };

    let engine_costs = costs.engine();
    let engine = costs.engine;
    let group = if timings.len() > 1 { Some(out.effects.len()) } else { None };
    for t in timings {
        let ask = !matches!(t, Timing::ActivateMain | Timing::Main | Timing::Counter);
        let (body, condition) = if costs.resolving() { costs.wrap(&action, ask) } else { (action.clone(), None) };
        let mut e = Effect::new(t, body, raw);
        e.once_per_turn = spec.once_per_turn;
        e.don_req = spec.don_req;
        e.your_turn = spec.your_turn;
        e.opp_turn = spec.opp_turn;
        e.rest_don = engine.0;
        e.don_minus = engine.1;
        e.rest_self = engine.2;
        e.condition = match (condition, &pre_q) {
            (c, None) => c,
            (None, Some(q)) => Some(Cond::Q(Box::new(q.clone()))),
            (Some(c), Some(q)) => Some(Cond::All(vec![c, Cond::Q(Box::new(q.clone()))])),
        };
        e.optional = engine_costs && ask;
        e.opt_group = group;
        out.effects.push(e);
    }
    Ok(())
}

/// The costs before a colon, by who pays them.
#[derive(Default)]
struct CostParts {
    /// paid on activation: rest DON!!, DON!! -X, rest this card
    engine: (u32, u32, bool),
    /// costs the first compiler reads, paid while resolving
    legacy: Option<Costs>,
    /// general actions paid in full while resolving
    acts: Vec<Act>,
    /// other steps paid first ("return 1 or more DON!!"), and what they need
    pre: Vec<Body>,
    pre_cond: Vec<Q>,
    /// steps right after the general costs ("... and shuffle it")
    post: Vec<Body>,
}

impl CostParts {
    fn resolving(&self) -> bool {
        self.legacy.is_some() || !self.acts.is_empty() || !self.pre.is_empty()
    }
    fn engine(&self) -> bool {
        self.engine.0 > 0 || self.engine.1 > 0 || self.engine.2
    }
    /// The effect's body with the resolving costs in front, and when they can be paid.
    fn wrap(&self, action: &Body, ask: bool) -> (Body, Option<Cond>) {
        let ask = ask && !self.engine();
        let mut body = action.clone();
        for p in self.post.iter().rev() {
            body = Body::Seq(Box::new(p.clone()), Box::new(body));
        }
        if !self.acts.is_empty() {
            body = Body::Pay { costs: self.acts.clone(), ask: ask && self.legacy.is_none() && self.pre.is_empty(), body: Box::new(body) };
        }
        for p in self.pre.iter().rev() {
            body = Body::Seq(Box::new(p.clone()), Box::new(body));
        }
        if !self.pre.is_empty() && self.legacy.is_none() && ask {
            body = Body::Optional(Box::new(body));
        }
        if let Some(k) = &self.legacy {
            body = Body::WithCosts { costs: Box::new(k.clone()), ask, body: Box::new(body) };
        }
        let mut conds: Vec<Cond> = vec![];
        if let Some(k) = &self.legacy {
            conds.push(Cond::Payable(Box::new(k.clone())));
        }
        if !self.acts.is_empty() {
            conds.push(Cond::Q(Box::new(Q::Payable(self.acts.clone()))));
        }
        for q in &self.pre_cond {
            conds.push(Cond::Q(Box::new(q.clone())));
        }
        let cond = match conds.len() {
            0 => None,
            1 => conds.pop(),
            _ => Some(Cond::All(conds)),
        };
        (body, cond)
    }
}

fn costs2(prefix: &str) -> Res<CostParts> {
    let mut c = CostParts::default();
    if let Ok(p) = parse_costs(prefix) {
        c.engine = (p.rest_don.unwrap_or(0), p.don_minus.unwrap_or(0), p.rest_self);
        if p.any_resolving {
            c.legacy = Some(p.k);
        }
        return Ok(c);
    }
    let mut legacy_parts: Vec<String> = vec![];
    let prefix = &prefix.replace("You can ", "You may ");
    let mut parts = split_list(prefix);
    if let Some(m) = parts.first().and_then(|p| rmatch(r"^(?P<c>[➀-➉①-⑩])\s*(?P<rest>.+)$", p)) {
        // "➀ You may rest this Character and 1 of your Characters": the DON!! cost, then the rest split again
        let (c, rest) = (m["c"].to_string(), m["rest"].to_string());
        let mut again = split_list(&rest);
        again.insert(0, c);
        again.extend(parts.drain(1..));
        parts = again;
    }
    if let Some(m) = parts.first().and_then(|p| rmatch(r"^(?P<c>[➀-➉①-⑩])\s*(?P<rest>.+)$", p)) {
        // "➀ You may add 1 card ...": a rest-DON!! cost, then more
        let (c, rest) = (m["c"].to_string(), m["rest"].to_string());
        parts[0] = rest;
        parts.insert(0, c);
    }
    for part in parts {
        // "Rest this Character and ...:" (no "You may"): the verb, not "DON!!"
        let p = part.trim();
        let p = if p.chars().nth(1).map_or(false, |c| c.is_lowercase()) { lower_first(p) } else { p.to_string() };
        let p = p.strip_prefix("and ").unwrap_or(&p);
        let p = p.strip_prefix("You may ").or_else(|| p.strip_prefix("you may ")).unwrap_or(p).to_string();
        let p = p.as_str();
        if p.is_empty() {
            continue;
        }
        if let Some(m) = rfull(r"rest your (?P<np>.+?) Leader or 1 of your DON!! cards", p) {
            // either cost: the Leader (if it fits and is active) or an active DON!!
            let mut pred = np(&format!("{} Leader", &m["np"]))?;
            pred.rested = Some(false);
            let leader = Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred: pred.clone() };
            c.pre.push(Body::Do(Do::RestLeaderOrDon(pred)));
            c.pre_cond.push(Q::Or(vec![Q::exists(leader), Q::Cmp(Num::DonActive(Side::Me), Cmp::Ge, Num::K(1))]));
            continue;
        }
        if rfull(r"shuffle (?:it|your deck)", p).is_some() {
            c.post.push(Body::Do(Do::Shuffle));
            continue;
        }
        if let Some(m) = rfull(r"trash this Character with a cost of (?P<n>\d+) or more", p) {
            c.acts.push(Act { sel: Sel::This, verb: Verb::Trash, opp_chooses: false });
            c.pre_cond.push(Q::This(Pred { cost: vec![(Cmp::Ge, Num::K(gi(&m, "n")), false)], ..Pred::default() }));
            continue;
        }
        if let Some(m) = rfull(r"return (?P<n>\d+) total of your currently given DON!! cards to your cost area rested", p) {
            c.pre.push(Body::Do(Do::ReturnGivenDon(gi(&m, "n") as u32)));
            c.pre_cond.push(Q::Cmp(Num::DonGiven(Side::Me), Cmp::Ge, Num::K(gi(&m, "n"))));
            continue;
        }
        if let Some(m) = rfull(r"return (?P<n>\d+) or more DON!! cards from your field to your DON!! deck", p) {
            c.pre.push(Body::Do(Do::ReturnDonAtLeast(gi(&m, "n") as u32)));
            c.pre_cond.push(Q::Cmp(Num::DonField(Side::Me), Cmp::Ge, Num::K(gi(&m, "n"))));
            continue;
        }
        if let Ok(x) = parse_costs(p).or_else(|_| parse_costs(&format!("You may {p}"))) {
            if !x.any_resolving {
                c.engine.0 += x.rest_don.unwrap_or(0);
                c.engine.1 += x.don_minus.unwrap_or(0);
                c.engine.2 |= x.rest_self;
                continue;
            }
            if let Ok(a) = cost_act(&cap(p)) {
                c.acts.push(a);
            } else {
                legacy_parts.push(p.to_string());
            }
            continue;
        }
        c.acts.push(cost_act(&cap(p))?);
    }
    if !legacy_parts.is_empty() {
        c.legacy = Some(parse_costs(&legacy_parts.join(", ")).map_err(|_| Unsupported)?.k);
    }
    Ok(c)
}

/// "A, B and C" -> parts, keeping "Leader or Character cards" and the like together.
fn split_list(s: &str) -> Vec<String> {
    let mut out = vec![];
    for a in s.split(", ") {
        let mut rest = a.to_string();
        // "rest this Character and 1 of your {X} type Leader or Stage cards": the verb twice
        if let Some(m) = rmatch(r"^(?P<v>(?:[Yy]ou may )?(?:rest|trash|return|place)) (?P<a>this \w+) and (?P<b>\d+ .+)$", &rest) {
            // "place this card and 1 card from your hand at the bottom of your deck": the place goes with both
            let tail = rmatch(r"^.+?(?P<t> (?:at the|to the|to your|to its owner's|to the owner's) .+)$", &m["b"]).map(|t| t["t"].to_string()).unwrap_or_default();
            out.push(format!("{} {}{tail}", &m["v"], &m["a"]));
            rest = format!("{} {}", &m["v"], &m["b"]);
        }
        // "rest this Character and trash 1 card": split before a verb
        while let Some(m) = rmatch(
            r"^(.+?) and ((?:you may )?(?:[Tt]rash|[Rr]est|[Rr]eturn|[Pp]lace|[Aa]dd|[Gg]ive|[Kk]\.O\.|[Rr]eveal|[Tt]urn|[Ss]huffle|[Dd]raw) .+)$",
            &rest,
        ) {
            out.push(m[1].to_string());
            rest = m[2].to_string();
        }
        out.push(rest);
    }
    out
}

/// One cost: an action that must be carried out in full ("trash 1 card from your hand").
fn cost_act(p: &str) -> Res<Act> {
    match clause_gen(p)? {
        Body::Act(mut a) => {
            // a cost is paid in full: "up to N" can't be paid with fewer
            if let Sel::Cards { many, side, .. } = &mut a.sel {
                if let Many::UpTo(n) = *many {
                    *many = Many::Exactly(n);
                }
                if *side == Side::Both {
                    *side = Side::Me; // costs are paid with your own cards
                }
            }
            Ok(*a)
        }
        _ => Err(Unsupported),
    }
}

// ---------------------------------------------------------------- permanent effects

fn static2(body: &str, spec: &Spec, out: &mut Compiled) -> Res<()> {
    if let Ok(()) = replacement(body, spec, out) {
        return Ok(());
    }
    let flag = |out: &mut Compiled, which: &str| match which {
        "rested" => out.play_rested = true,
        "on play" => out.no_own_on_play = true,
        "don" => out.don_phase_to_leader = true,
        "life" => out.faceup_life_to_deck = true,
        _ => out.no_effect_play = true,
    };
    for (pat, which) in [
        (r"Your Character cards are played rested\.", "rested"),
        (r"Your \[On Play\] effects are negated\.", "on play"),
        (r"(?:If you have any DON!! cards on your field, )?1 DON!! card placed during your DON!! Phase is given to your Leader\.", "don"),
        (r"Your face-up Life cards are placed at the bottom of your deck instead of being added to your hand, according to the rules\.", "life"),
        (r"This card in your hand cannot be played by effects\.", "play"),
    ] {
        if rfull(pat, body.trim()).is_some() {
            flag(out, which);
            return Ok(());
        }
    }
    if rfull(r"Once per turn, this Character cannot be K\.O\.'d by your opponent's effects\.", body.trim()).is_some() {
        let r = RepData {
            who: None, ko_only: true, battle: Some(false), by_opp: true, cond: None, cost: vec![], rest_this: false, or_ko: false,
            don_return: 0, don_rest: 0, after: vec![], optional: false, your_turn: spec.your_turn, opp_turn: spec.opp_turn,
        };
        out.gen_replacements.push((r, "It is not K.O.'d".into(), true));
        return Ok(());
    }
    if rfull(r"Under the rules of this game, you (?:may|can) have (?:any number|up to \d+) of this card in your deck\.?", body.trim()).is_some() {
        return Ok(()); // deck building only
    }
    if let Some(m) = rfull(r"Under the rules of this game, also treat this card's name as (?P<n>\[[^\]]+\](?: and \[[^\]]+\])*)\.", body.trim()) {
        out.aliases.extend(crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]));
        return Ok(());
    }
    if rfull(r"Under the rules of this game, you do not lose when your deck has 0 cards\. You lose at the end of the turn in which your deck becomes 0 cards\.", body.trim()).is_some() {
        out.deck_out_at_end = true;
        return Ok(());
    }
    if let Some(m) = rfull(r"Under the rules of this game, your DON!! deck consists of (?P<n>\d+) cards\.", body.trim()) {
        out.don_deck_size = Some(gi(&m, "n") as u32);
        return Ok(());
    }
    if rfull(r"When your deck is reduced to 0, you win the game instead of losing, according to the rules\.", body.trim()).is_some() {
        out.deck_out_wins = true;
        return Ok(());
    }
    // deck-building restrictions: checked when a deck is built, not during the game
    if let Some(m) = rfull(r"Under the rules of this game, you (?:cannot|can only) include [^.]+? in your deck(?P<rest> and .+)?\.", body.trim()) {
        match g(&m, "rest").map(str::trim) {
            None => return Ok(()),
            Some("and when your deck is reduced to 0, you win the game instead of losing") => {
                out.deck_out_wins = true;
                return Ok(());
            }
            Some(r) => {
                if let Some(x) = rfull(r"and at the start of the game, (?P<b>.+)", r) {
                    let b = body2(&cap(&x["b"]))?;
                    out.effects.push(Effect::new(Timing::StartOfGame, b, "At the start of the game"));
                    return Ok(());
                }
                return Err(Unsupported);
            }
        }
    }
    if let Some(m) = rfull(r"Also treat this card's name as \[(?P<n>[^\]]+)\] according to the rules\.", body.trim()) {
        out.aliases.push(m["n"].to_string());
        return Ok(());
    }
    // several sentences: each a permanent effect under the same tags
    let ss = sentences(body.trim());
    if ss.len() > 1 {
        let mut tmp = Compiled::default();
        for s in &ss {
            static2(s, spec, &mut tmp)?;
        }
        merge_statics(out, tmp);
        return Ok(());
    }
    let mut body = body.trim().to_string();
    let mut extra: Option<Box<Cond>> = None;
    if let Some((q, rest)) = lead_condition(&body) {
        extra = Some(Box::new(Cond::Q(Box::new(q))));
        body = cap(&rest);
    }
    let cond = Cond::Spec { don_req: spec.don_req, your_turn: spec.your_turn, opp_turn: spec.opp_turn, extra };
    let body = body.trim_end_matches('.');

    if let Some(m) = rfull(r"This Character cannot attack unless (?P<c>.+)", body) {
        let q = cond2(&m["c"])?;
        out.static_keywords.push((and_cond(cond, Q::Not(Box::new(q))), Kw::CannotAttack, Target::SelfCard));
        return Ok(());
    }
    if rfull(r"This Character cannot attack a Leader on the turn in which it is played", body).is_some() {
        out.static_keywords.push((cond, Kw::NoLeaderFirstTurn, Target::SelfCard));
        return Ok(());
    }
    if rfull(r"Your opponent cannot attack any card other than this Character", body).is_some() {
        out.static_keywords.push((cond, Kw::Taunt, Target::SelfCard));
        return Ok(());
    }
    if let Some(m) = rfull(r"Your (?P<ty>\{[^}]+\}) type Leader's base power becomes (?P<v>\d+)", body) {
        let pred = Pred { cats: vec![Category::Leader], types: braces(&m["ty"]), ..Pred::default() };
        out.gen_base_power.push((cond, Num::K(gi(&m, "v")), Target::Sel { side: Side::Me, pred }));
        return Ok(());
    }
    if let Some(m) = rfull(r"(?P<a>.+?), and (?P<b>(?:all of )?your .+)", body) {
        let mut tmp = Compiled::default();
        if static_who(&m["a"], &cond, &mut tmp).is_ok() && static_who(&cap(&m["b"]), &cond, &mut tmp).is_ok() {
            merge_statics(out, tmp);
            return Ok(());
        }
    }
    if let Some(m) = rfull(r"[Ss]et the base power of all of your (?P<np>.+?) to (?P<v>\d+)", body) {
        let pred = np(&m["np"])?;
        out.gen_base_power.push((cond, Num::K(gi(&m, "v")), Target::Sel { side: Side::Me, pred }));
        return Ok(());
    }
    if let Some(m) = rfull(r"All of your (?P<np>.+?) without a Counter have a \+(?P<v>\d+) Counter, according to the rules", body) {
        let mut pred = np(&m["np"])?;
        pred.counter = Some((Cmp::Eq, 0));
        out.gen_counter.push((cond, pred, gi(&m, "v")));
        return Ok(());
    }
    if let Some(m) = rfull(r"The counter of all of your (?P<np>.+?) in your hand becomes \+(?P<v>\d+)", body) {
        out.gen_counter.push((cond, np(&m["np"])?, gi(&m, "v")));
        return Ok(());
    }
    if let Some(m) = rfull(r"This Character gains (?:\+(?P<p>\d+) power|\[(?P<kw>[^\]]+)\]) and \+(?P<c>\d+) cost for every (?P<k>\d+) (?P<w>.+)", body) {
        let per = counted(Side::Me, &m["w"])?;
        let k = gi(&m, "k");
        if g(&m, "p").is_some() {
            out.gen_power.push((cond.clone(), Num::Per(Box::new(per.clone()), k, gi(&m, "p")), Target::SelfCard));
        }
        if let Some(kw) = g(&m, "kw") {
            out.static_keywords.push((cond.clone(), Kw::printed(kw).ok_or(Unsupported)?, Target::SelfCard));
        }
        out.gen_cost.push((cond, Num::Per(Box::new(per), k, gi(&m, "c")), Target::SelfCard));
        return Ok(());
    }
    if let Some(m) = rfull(r"This Character gains \+(?P<v>\d+) power for each of your Characters with a different card name", body) {
        let s = Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred: Pred { cats: vec![Category::Character], ..Pred::default() } };
        out.gen_power.push((cond, Num::Per(Box::new(Num::DistinctNames(Box::new(s))), 1, gi(&m, "v")), Target::SelfCard));
        return Ok(());
    }
    if let Some(m) = rfull(r"Your opponent cannot attack any card other than the Character (?P<n>\[[^\]]+\])", body) {
        let pred = Pred { cats: vec![Category::Character], names: crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), ..Pred::default() };
        out.static_keywords.push((cond, Kw::Taunt, Target::Sel { side: Side::Me, pred }));
        return Ok(());
    }
    if let Some(m) = rfull(r"(?P<np>.+?) other than your (?P<n>\[[^\]]+\]) cannot be K\.O\.'d in battle", body) {
        let mut pred = np(&m["np"])?;
        pred.not_names.extend(crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]));
        out.static_keywords.push((cond, Kw::NoKoInBattle, Target::Sel { side: Side::Me, pred }));
        return Ok(());
    }
    if let Some(m) = rfull(r"[Aa]ll Characters with a cost of (?P<n>\d+) or less do not become active in your and your opponent's Refresh Phases", body) {
        let pred = Pred { cats: vec![Category::Character], cost: vec![(Cmp::Le, Num::K(gi(&m, "n")), false)], ..Pred::default() };
        out.static_keywords.push((cond, Kw::StayRested, Target::Sel { side: Side::Both, pred }));
        return Ok(());
    }
    if let Some(m) = rfull(r"[Aa]ll Characters with a cost of (?P<a>\d+) or (?P<b>\d+) cannot attack", body) {
        let one = |v| Pred { cost: vec![(Cmp::Eq, Num::K(v), false)], ..Pred::default() };
        let pred = Pred { cats: vec![Category::Character], any_of: vec![one(gi(&m, "a")), one(gi(&m, "b"))], ..Pred::default() };
        out.static_keywords.push((cond, Kw::CannotAttack, Target::Sel { side: Side::Both, pred }));
        return Ok(());
    }
    if let Some(m) = rfull(r"This Character cannot be K\.O\.'d by effects of (?P<o>your opponent's )?(?P<np>.+)", body) {
        let mut pred = np(&m["np"])?;
        pred.opp_only = g(&m, "o").is_some();
        out.no_ko_effect_by.push((cond, Target::SelfCard, pred));
        return Ok(());
    }
    if let Some(m) = rfull(r"All of your (?P<n>\[[^\]]+\]) cards' base power and this Character's base power become (?P<v>\d+)", body) {
        let pred = Pred { cats: vec![Category::Character], names: crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), ..Pred::default() };
        out.gen_base_power.push((cond.clone(), Num::K(gi(&m, "v")), Target::Sel { side: Side::Me, pred }));
        out.gen_base_power.push((cond, Num::K(gi(&m, "v")), Target::SelfCard));
        return Ok(());
    }
    if let Some(m) = rfull(r"[Tt]his Character's base power becomes (?P<v>\d+)(?: and it gains \+(?P<c>\d+) cost)?", body) {
        out.gen_base_power.push((cond.clone(), Num::K(gi(&m, "v")), Target::SelfCard));
        if g(&m, "c").is_some() {
            out.static_cost.push((cond, gi(&m, "c"), Target::SelfCard));
        }
        return Ok(());
    }
    if let Some(m) = rfull(r"[Dd]uring your opponent's turn, your Leader's base power becomes (?P<v>\d+)", body) {
        let pred = Pred { cats: vec![Category::Leader], ..Pred::default() };
        out.gen_base_power.push((and_cond(cond, Q::OppTurn), Num::K(gi(&m, "v")), Target::Sel { side: Side::Me, pred }));
        return Ok(());
    }
    if let Some(m) = rfull(r"During the turn in which a card in your hand is trashed by an effect, give this card in your hand -(?P<n>\d+) cost", body) {
        out.hand_cost.push((and_cond(cond, Q::HandTrashedThisTurn), -gi(&m, "n")));
        return Ok(());
    }
    if rfull(r"This Character's base power becomes the same as your Leader's base power", body).is_some() {
        out.gen_base_power.push((cond, Num::LeaderBase(Side::Me), Target::SelfCard));
        return Ok(());
    }
    if let Some(m) = rfull(r"Give (?P<np>.+?) in your hand -(?P<n>\d+) cost", body).filter(|m| &m["np"] != "this card") {
        let mut pred = np(&m["np"])?;
        for c in pred.cost.iter_mut() {
            c.2 = true;
        }
        out.hand_discount.push((cond, pred, gi(&m, "n")));
        return Ok(());
    }
    if let Some(m) = rfull(r"The cost of playing (?P<np>.+?) from your hand will be reduced by (?P<n>\d+)", body) {
        let mut pred = np(&m["np"])?;
        for c in pred.cost.iter_mut() {
            c.2 = true; // the printed cost: the reduction can't read itself
        }
        out.hand_discount.push((cond, pred, gi(&m, "n")));
        return Ok(());
    }
    if let Some(m) = rfull(r"Give this card in your hand -(\d+) cost", body) {
        out.hand_cost.push((cond, -m[1].parse::<i32>().unwrap()));
        return Ok(());
    }
    if let Some(m) = rfull(r"Give all of your opponent's (?P<np>.+?) -(?P<v>\d+) (?P<w>power|cost)", body) {
        let pred = np(&m["np"])?;
        let to = Target::Sel { side: Side::Opp, pred };
        if &m["w"] == "cost" {
            out.static_cost.push((cond, -gi(&m, "v"), to));
        } else {
            out.static_power.push((cond, -gi(&m, "v"), to));
        }
        return Ok(());
    }
    if let Some(m) = rfull(r"(?P<who>.+?) gains? \+(?P<v>\d+) power for every (?:(?P<k>\d+) )?(?:of your )?(?P<what>.+)", body) {
        let to = static_target(&m["who"])?;
        let per = counted(Side::Me, &m["what"])?;
        let k = g(&m, "k").map_or(1, |k| k.parse().unwrap());
        out.gen_power.push((cond, Num::Per(Box::new(per), k, gi(&m, "v")), to));
        return Ok(());
    }
    if let Some(m) = rfull(r"Give (?P<who>this (?:Character|Leader)) (?P<sg>[+-])(?P<v>\d+) (?P<w>power|cost)", body) {
        let v = gi(&m, "v") * if &m["sg"] == "-" { -1 } else { 1 };
        let to = static_target(&m["who"])?;
        if &m["w"] == "cost" {
            out.static_cost.push((cond, v, to));
        } else {
            out.static_power.push((cond, v, to));
        }
        return Ok(());
    }
    // "This Character gains [Double Attack] if you have 5 or more cards in your hand."
    if let Some(m) = rfull(r"(?P<b>.+?) if (?P<c>.+)", body) {
        if let Ok(q) = cond2(&m["c"]) {
            let cond = and_cond(cond.clone(), q);
            let mut tmp = Compiled::default();
            if static_who(&m["b"], &cond, &mut tmp).is_ok() {
                merge_statics(out, tmp);
                return Ok(());
            }
        }
    }
    static_who(body, &cond, out)
}

fn and_cond(cond: Cond, q: Q) -> Cond {
    match cond {
        Cond::Spec { don_req, your_turn, opp_turn, extra } => {
            let extra = Some(Box::new(match extra {
                Some(e) => Cond::All(vec![*e, Cond::Q(Box::new(q))]),
                None => Cond::Q(Box::new(q)),
            }));
            Cond::Spec { don_req, your_turn, opp_turn, extra }
        }
        c => Cond::All(vec![c, Cond::Q(Box::new(q))]),
    }
}

fn merge_statics(out: &mut Compiled, tmp: Compiled) {
    out.static_keywords.extend(tmp.static_keywords);
    out.static_power.extend(tmp.static_power);
    out.static_cost.extend(tmp.static_cost);
    out.no_ko_battle_by.extend(tmp.no_ko_battle_by);
    out.gen_base_power.extend(tmp.gen_base_power);
    out.gen_power.extend(tmp.gen_power);
    out.gen_cost.extend(tmp.gen_cost);
    out.gen_counter.extend(tmp.gen_counter);
    out.no_ko_effect_by.extend(tmp.no_ko_effect_by);
    out.hand_cost.extend(tmp.hand_cost);
    out.gen_replacements.extend(tmp.gen_replacements);
    out.play_rested |= tmp.play_rested;
    out.no_own_on_play |= tmp.no_own_on_play;
    out.don_phase_to_leader |= tmp.don_phase_to_leader;
    out.faceup_life_to_deck |= tmp.faceup_life_to_deck;
    out.no_effect_play |= tmp.no_effect_play;
}

/// "<who> gains ... / cannot ...", for one or more targets ("Your Leader and all of your Characters").
fn static_who(body: &str, cond: &Cond, out: &mut Compiled) -> Res<()> {
    let Some(m) = rmatch(r"^(?P<who>.+?) (?P<rest>gains? .+|cannot .+|can also attack .+|can attack .+)$", body) else {
        return Err(Unsupported);
    };
    let who = &m["who"];
    let mut targets = vec![];
    match static_target(who) {
        Ok(t) => targets.push(t),
        Err(_) => {
            let x = rfull(r"(?P<a>.+?) and (?P<b>(?:all of |all )?(?:your|this) .+)", who).ok_or(Unsupported)?;
            targets.push(static_target(&x["a"])?);
            targets.push(static_target(&x["b"].replace("all your ", "all of your "))?);
        }
    }
    let mut tmp = Compiled::default();
    for to in targets {
        static_effect(&m["rest"], cond, &to, &mut tmp)?;
    }
    merge_statics(out, tmp);
    Ok(())
}

/// What a permanent effect does to its target; "A and B" does both.
fn static_effect(rest: &str, cond: &Cond, to: &Target, out: &mut Compiled) -> Res<()> {
    let (cond, to) = (cond.clone(), to.clone());
    for (i, _) in rest.match_indices(" and ") {
        let (a, b) = (&rest[..i], &rest[i + 5..]);
        if rmatch(r"^(?:gains? |cannot |can )", b).is_some() || rmatch(r"^[+-]\d+ (?:power|cost)$", b).is_some() {
            let b = if b.starts_with('+') || b.starts_with('-') { format!("gains {b}") } else { b.to_string() };
            let mut tmp = Compiled::default();
            if static_effect(a, &cond, &to, &mut tmp).is_ok() && static_effect(&b, &cond, &to, &mut tmp).is_ok() {
                merge_statics(out, tmp);
                return Ok(());
            }
        }
    }
    if let Some(km) = rfull(r"cannot be K\.O\.'d in battle by (?P<np>.+)", rest) {
        let mut pred = np(&km["np"])?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Leader, Category::Character];
        }
        out.no_ko_battle_by.push((cond, to, pred));
        return Ok(());
    }
    if rfull(r"can attack Characters on the turn in which (?:it is|they are) played", rest).is_some() {
        out.static_keywords.push((cond, Kw::RushCharacter, to));
        return Ok(());
    }
    if rfull(r"can also attack (?:your opponent's )?active Characters", rest).is_some() {
        out.static_keywords.push((cond, Kw::AttackActive, to));
        return Ok(());
    }
    if let Some(km) = rfull(r"cannot be K\.O\.'d (?P<how>in battle|by effects|by your opponent's effects)", rest) {
        let k = match &km["how"] {
            "in battle" => Kw::NoKoInBattle,
            "by effects" => Kw::NoKoByEffects,
            _ => Kw::NoKoByOppEffects,
        };
        out.static_keywords.push((cond, k, to));
        return Ok(());
    }
    if rfull(r"cannot be removed from the field by your (?:opponent's )?effects", rest).is_some() {
        // "All of your opponent's Characters cannot be removed from the field by your effects": by their opponent
        out.static_keywords.push((cond, Kw::NoRemoveByOpp, to));
        return Ok(());
    }
    if rfull(r"cannot be K\.O\.'d or rested by your opponent's effects", rest).is_some() {
        out.static_keywords.push((cond.clone(), Kw::NoKoByOppEffects, to.clone()));
        out.static_keywords.push((cond, Kw::NoRestByOpp, to));
        return Ok(());
    }
    if rfull(r"cannot be rested by your opponent's effects", rest).is_some() {
        out.static_keywords.push((cond, Kw::NoRestByOpp, to));
        return Ok(());
    }
    if rfull(r"cannot attack", rest).is_some() {
        out.static_keywords.push((cond, Kw::CannotAttack, to));
        return Ok(());
    }
    let parts = rfull(r"gains? (?:\[(?P<kw>[^\]]+)\])?(?: and )?(?:(?P<sg>[+-])(?P<v>\d+) (?P<what>power|cost))?", rest)
        .ok_or(Unsupported)?;
    if g(&parts, "kw").is_none() && g(&parts, "v").is_none() {
        return Err(Unsupported);
    }
    if let Some(k) = g(&parts, "kw") {
        let k = Kw::printed(k).ok_or(Unsupported)?;
        out.static_keywords.push((cond.clone(), k, to.clone()));
    }
    if g(&parts, "v").is_some() {
        let v = gi(&parts, "v") * if &parts["sg"] == "-" { -1 } else { 1 };
        if &parts["what"] == "power" {
            out.static_power.push((cond, v, to));
        } else {
            out.static_cost.push((cond, v, to));
        }
    }
    Ok(())
}

/// Who a permanent effect applies to.
fn static_target(who: &str) -> Res<Target> {
    let w = who.trim();
    if rfull(r"(?:This|this) (?:Character|Leader|card|Stage)", w).is_some() {
        return Ok(Target::SelfCard);
    }
    if let Some(m) = rfull(r"(?:All of |all of )?(?:[Yy]our) (?P<opp>opponent's )?(?P<np>.+)", w) {
        let mut pred = np(&m["np"])?;
        if pred.cats.is_empty() && !pred.names.is_empty() {
            pred.cats = vec![Category::Leader, Category::Character]; // "Your [Blugori]", "your [Ohm] cards"
        }
        if pred.cats.is_empty() {
            return Err(Unsupported);
        }
        if m["np"].contains("other than this") {
            pred.not_this = true;
        }
        let side = if g(&m, "opp").is_some() { Side::Opp } else { Side::Me };
        return Ok(Target::Sel { side, pred });
    }
    Err(Unsupported)
}

/// "[Once Per Turn] If this Character would be K.O.'d by an effect, you may trash 1 card from
/// your hand instead."
fn replacement(body: &str, spec: &Spec, out: &mut Compiled) -> Res<()> {
    let m = rfull(
        concat!(
            r"If (?P<who>.+?) would (?:be )?(?P<what>K\.O\.'d|removed from the field|leave the field)",
            r"(?P<how> in battle| by an effect| by effects| by your opponent's effects?| by your opponent)?",
            r"(?P<orko> or (?:would be )?K\.O\.'d| or would be removed from the field by your opponent's effects?)?, ",
            r"(?P<may>you may )?(?P<cost>.+?) instead(?: of that Character being K\.O\.'d)?\.(?: If there is an? (?P<neg>.+?), this effect is negated\.)?"
        ),
        body.trim(),
    )
    .ok_or(Unsupported)?;
    let mut who = m["who"].trim().to_string();
    let mut cond = None;
    if let Some(n) = g(&m, "neg") {
        // "If there is a [Monkey.D.Luffy] Character, this effect is negated."
        let mut pred = np(n)?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        cond = Some(Q::Not(Box::new(Q::exists(Sel::Cards { side: Side::Both, place: Place::Field, many: Many::All, pred }))));
    }
    // "If you have a green Character other than [Tashigi] that would be removed ...": one of yours
    if let Some(x) = rfull(r"you have an? (?P<np>.+?) that", &who) {
        who = format!("your {}", &x["np"]);
    }
    // "If your Leader has the {X} type and this Character would be ..."
    for (i, _) in m["who"].match_indices(" and ") {
        if let Ok(q) = cond2(&m["who"][..i]) {
            cond = Some(q);
            who = m["who"][i + 5..].trim().to_string();
            break;
        }
    }
    let who = who.as_str();
    let who = if rfull(r"this (?:Character|Stage)", who).is_some() {
        None
    } else {
        let rest = who.strip_prefix("your ").or_else(|| who.strip_prefix("one of your ")).ok_or(Unsupported)?;
        let fixed;
        let rest = match rfull(r"Character (?P<n>\[[^\]]+\])", rest) {
            Some(x) => {
                fixed = format!("{} Character", &x["n"]);
                fixed.as_str()
            }
            None => rest,
        };
        let mut pred = np(rest)?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        Some(pred)
    };
    let mut how = g(&m, "how").unwrap_or("").trim().to_string();
    let orko = g(&m, "orko").unwrap_or("");
    let mut what = m["what"].to_string();
    let mut or_ko = false;
    if orko.contains("K.O.") {
        or_ko = true; // "removed from the field by your opponent's effect or K.O.'d"
    } else if !orko.is_empty() {
        // "K.O.'d or would be removed from the field by your opponent's effect"
        or_ko = true;
        what = "removed from the field".into();
        how = "by your opponent's effect".into();
    }
    let how = how.as_str();
    let mut rest_this = false;
    let mut don_return = 0;
    let mut don_rest = 0;
    let mut after = vec![];
    let mut cost = vec![];
    for part in split_list(&m["cost"]) {
        let p = part.trim();
        if rfull(r"rest this (?:Character|Stage)", p).is_some() {
            rest_this = true;
            continue;
        }
        if let Some(d) = rfull(r"return (?P<n>\d+) DON!! cards? from your field to your DON!! deck", p) {
            don_return = gi(&d, "n") as u32;
            continue;
        }
        if let Some(d) = rfull(r"rest (?P<n>\d+) of your (?:active )?DON!! cards?", p) {
            don_rest = gi(&d, "n") as u32;
            continue;
        }
        match cost_act(&cap(p)) {
            Ok(a) => cost.push(a),
            Err(_) => after.push(clause_gen(&cap(p))?), // "... and draw 1 card instead": not a cost, done after
        }
    }
    let text = format!("{} instead", cap(&m["cost"]));
    out.gen_replacements.push((
        RepData {
            who,
            ko_only: what.starts_with("K.O."),
            battle: match how {
                "in battle" => Some(true),
                "" => None,
                _ => Some(false),
            },
            by_opp: how.contains("opponent"),
            cond,
            cost,
            rest_this,
            or_ko,
            don_return,
            don_rest,
            after,
            optional: m.name("may").is_some(),
            your_turn: spec.your_turn,
            opp_turn: spec.opp_turn,
        },
        text,
        spec.once_per_turn,
    ));
    Ok(())
}

// ---------------------------------------------------------------- bodies

/// Split text into sentences ("K.O." doesn't end one).
fn sentences(text: &str) -> Vec<String> {
    let b = text.as_bytes();
    let mut out = vec![];
    let mut start = 0;
    let mut depth = 0i32;
    for i in 0..b.len() {
        match b[i] {
            b'[' => depth += 1,
            b']' => depth -= 1,
            _ => {}
        }
        if depth == 0 && b[i] == b'.' && i + 2 < b.len() && b[i + 1] == b' ' && b[i + 2].is_ascii_uppercase() {
            if i >= 3 && &text[i - 3..i] == "K.O" {
                continue;
            }
            out.push(text[start..=i].trim().to_string());
            start = i + 2;
        }
    }
    let last = text[start..].trim();
    if !last.is_empty() {
        out.push(last.to_string());
    }
    out
}

pub fn body2(text: &str) -> Res<Body> {
    let ss = sentences(text.trim());
    if ss.is_empty() {
        return Err(Unsupported);
    }
    let mut parts: Vec<Body> = vec![];
    let mut i = 0;
    while i < ss.len() {
        let s = strip_then(&ss[i]);
        let next = ss.get(i + 1).map(|x| strip_then(x));
        if let Some((b, used)) = look(&s, next.as_deref()) {
            parts.push(b);
            i += used;
            continue;
        }
        if let Some((q, rest)) = lead_condition(s.trim_end_matches('.')) {
            if let Some((b, used)) = look(&cap(&rest), next.as_deref()) {
                parts.push(Body::If(Cond::Q(Box::new(q)), Box::new(b)));
                i += used;
                continue;
            }
        }
        if let (Some(x), Some(y)) = (s.strip_prefix("Your opponent may "), next.as_deref().and_then(|n| n.strip_prefix("If they do not, "))) {
            let x = x.trim_end_matches('.').replace("their ", "your opponent's ");
            let inner = match rfull(r"return (?P<n>\d+) of your opponent's active DON!! cards? to your opponent's DON!! deck", &x) {
                Some(m) => Body::Do(Do::OppReturnActiveDon(gi(&m, "n") as u32)),
                None => clause_gen(&cap(&x))?,
            };
            parts.push(Body::OppMayElse(Box::new(inner), Box::new(sentence(&cap(y))?)));
            i += 2;
            continue;
        }
        if let (Some(a), Some(b)) = (
            rfull(r"If (?P<a>.+?), draw 1 card\.", &s),
            next.as_deref().and_then(|n| rfull(r"If (?P<b>.+?), you may add up to 1 card from the top of your deck to the top of your Life cards instead of drawing 1 card\.", n)),
        ) {
            // two sentences about one draw: the second replaces it
            let (qa, qb) = (cond2(&a["a"])?, cond2(&b["b"])?);
            let choice = Body::ChooseOne {
                opponent: false,
                options: vec![("Add to Life instead".into(), Body::Do(Do::DeckToLife(1))), ("Draw 1 card".into(), Body::Do(Do::Draw(1)))],
            };
            let inner = Body::Seq(
                Box::new(Body::If(Cond::Q(Box::new(qb.clone())), Box::new(choice))),
                Box::new(Body::If(Cond::Q(Box::new(Q::Not(Box::new(qb)))), Box::new(Body::Do(Do::Draw(1))))),
            );
            parts.push(Body::If(Cond::Q(Box::new(qa)), Box::new(inner)));
            i += 2;
            continue;
        }
        if let (Some(a), Some(b)) = (
            rfull(r"(?:Choose up to 1 of your opponent's Characters with a cost of (?P<a>\d+) or less and K\.O\. it|K\.O\. up to 1 of your opponent's Characters with a cost of (?P<a2>\d+) or less)\.", &s),
            next.as_deref().and_then(|n| rfull(r"If (?P<c>.+?), (?:you may select|choose up to 1 of) your opponent's Characters? with a cost of (?P<b>\d+) or less instead(?: of a Character with a cost of \d+ or less)?\.", n)),
        ) {
            // the cost limit depends on a condition
            let lo: i32 = g(&a, "a").or_else(|| g(&a, "a2")).unwrap().parse().unwrap();
            let q = cond2(&b["c"])?;
            let pred = Pred { cats: vec![Category::Character], cost: vec![(Cmp::Le, Num::IfQ(Box::new(q), gi(&b, "b"), lo), false)], ..Pred::default() };
            parts.push(act(Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::UpTo(1), pred }, Verb::Ko));
            i += 2;
            continue;
        }
        if let (Some(_), Some(_)) = (
            rfull(r"(?:If .+?, )?[Ss]elect your opponent's rested Leader and up to 1 Character card\.", &s),
            next.as_deref().and_then(|n| rfull(r"The selected cards will not become active in your opponent's next Refresh Phase\.", n)),
        ) {
            let leader = Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::All, pred: Pred { cats: vec![Category::Leader], rested: Some(true), ..Pred::default() } };
            let ch = Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::UpTo(1), pred: Pred { cats: vec![Category::Character], ..Pred::default() } };
            let mut b = Body::Seq(Box::new(act(leader, Verb::NoRefresh)), Box::new(act(ch, Verb::NoRefresh)));
            if let Some((q, _)) = lead_condition(s.trim_end_matches('.')) {
                b = Body::If(Cond::Q(Box::new(q)), Box::new(b));
            }
            parts.push(b);
            i += 2;
            continue;
        }
        if let (Some(a), Some(_)) = (
            rfull(r"[Cc]hoose up to 1 (?P<a>Character card .+?) and up to 1 (?P<b>Character card .+?) from your trash\.", &s),
            next.as_deref().and_then(|n| rfull(r"Play 1 card and play the other card rested\.", n)),
        ) {
            parts.push(Body::Do(Do::PlayPairOneRested(np(&a["a"])?, np(&a["b"])?)));
            i += 2;
            continue;
        }
        if let Some(y) = next.as_deref().and_then(|n| n.strip_prefix("If you do, ").or_else(|| n.strip_prefix("If you do so, "))) {
            let then = sentence(&cap(y))?;
            let b = if let Some(x) = s.strip_prefix("You may ") {
                match cost_acts(x.trim_end_matches('.')) {
                    Ok(acts) => Body::Pay { costs: acts, ask: true, body: Box::new(then) },
                    Err(_) => Body::IfDid(Box::new(Body::Optional(Box::new(sentence(&cap(x))?))), Box::new(then)),
                }
            } else {
                Body::IfDid(Box::new(sentence(&s)?), Box::new(then))
            };
            parts.push(b);
            i += 2;
            continue;
        }
        parts.push(sentence(&s)?);
        i += 1;
    }
    let mut it = parts.into_iter().rev();
    let mut acc = it.next().unwrap();
    for p in it {
        acc = Body::Seq(Box::new(p), Box::new(acc));
    }
    Ok(acc)
}

fn strip_then(s: &str) -> String {
    let s = s.trim();
    cap(s.strip_prefix("Then, ").or_else(|| s.strip_prefix("Then ")).unwrap_or(s))
}

fn cost_acts(x: &str) -> Res<Vec<Act>> {
    split_list(x).iter().map(|p| cost_act(&cap(p.trim()))).collect()
}

/// One sentence.
fn sentence(s: &str) -> Res<Body> {
    let s = s.trim();
    let s = if s.ends_with('.') { s.to_string() } else { format!("{s}.") };
    if let Ok(b) = parse_body(&s) {
        return Ok(b);
    }
    let core = s.trim_end_matches('.');
    if rfull(r"[Ii]f the selected (?:card|Character) attacks during this turn, your opponent cannot activate \[Blocker\]", core).is_some() {
        return Ok(act(Sel::It, Verb::Grant(Kw::Unblockable, Until::Turn)));
    }
    if rfull(r"If any of your Characters would be K\.O\.'d in battle during this turn, you may trash 1 card from your hand instead", core).is_some() {
        return Ok(Body::Do(Do::BattleKoShield));
    }
    if let Some(m) = rfull(r"[Aa]t the end of this battle, (?P<v>place|trash|return) this Character(?P<w> at the bottom of the owner's deck| to the owner's hand)?", core) {
        let lv = match (&m["v"], g(&m, "w")) {
            ("place", Some(_)) => LaterVerb::ToDeckBottom,
            ("return", Some(_)) => LaterVerb::ToHand,
            _ => LaterVerb::Trash,
        };
        return Ok(act(Sel::This, Verb::AtBattleEnd(lv)));
    }
    if let Some(m) = rfull(r"[Aa]t the end of this turn, (?P<rest>.+)", core) {
        if rfull(r"return DON!! cards from your field to your DON!! deck until you have the same number of DON!! cards on your field as your opponent", &m["rest"]).is_some() {
            return Ok(Body::Do(Do::LaterDonMatch));
        }
        if let Some(x) = rfull(r"up to 1 (?P<np>rested Character.*?) will not become active in your opponent's next Refresh Phase", &m["rest"]) {
            return Ok(Body::Do(Do::LaterNoRefreshOne(np(&x["np"])?)));
        }
    }
    if let Some((q, rest)) = lead_condition(core) {
        return Ok(Body::If(Cond::Q(Box::new(q)), Box::new(sentence(&cap(&rest))?)));
    }
    // "Draw 1 card if you have 4 or less cards in your hand."
    for (i, _) in core.match_indices(" if ") {
        if let Ok(q) = cond2(&core[i + 4..]) {
            if let Ok(b) = clauses(&core[..i]) {
                return Ok(Body::If(Cond::Q(Box::new(q)), Box::new(b)));
            }
        }
    }
    if let Some(x) = core.strip_prefix("You may ") {
        return Ok(Body::Optional(Box::new(clauses(&cap(x))?)));
    }
    clauses(core)
}

/// A condition at the start: "If A and B, rest" -> (condition, rest).
fn lead_condition(text: &str) -> Option<(Q, String)> {
    // the longest prefix that reads as a condition: "If A, B and C, do X"
    let t = text.strip_prefix("If ")?;
    let cuts: Vec<usize> = t.match_indices(", ").map(|(i, _)| i).collect();
    for &i in cuts.iter().rev() {
        if let Ok(q) = cond2(&t[..i]) {
            return Some((q, t[i + 2..].to_string()));
        }
    }
    None
}

/// Actions joined by ", then", " and " or ", and ".
fn clauses(c: &str) -> Res<Body> {
    let c = c.trim().trim_end_matches('.');
    if let Ok(b) = clause(c) {
        return Ok(b);
    }
    let dur = rmatch(&format!(r"^.+?(?P<d>{})$", &DUR[6..DUR.len() - 2]), c).and_then(|m| g(&m, "d").map(String::from));
    for sep in [", then ", " and then ", " and, ", ", and ", " and ", ", "] {
        for (i, _) in c.match_indices(sep) {
            let (a, b) = (&c[..i], &c[i + sep.len()..]);
            // "... cannot be K.O.'d in battle and gains +2000 power until ...": the duration goes with both
            let first = clause(a).or_else(|e| dur.as_ref().map_or(Err(e), |d| clause(&format!("{a}{d}"))));
            let Ok(first) = first else { continue };
            let a_dur = rmatch(&format!(r"^.+?(?P<d>{})$", &DUR[6..DUR.len() - 2]), a).and_then(|m| g(&m, "d").map(String::from));
            if rmatch(r"^(?:gains?|cannot|can) ", b).is_some() {
                if let Some(subj) = rmatch(r"^(?:This Character|This Leader|That Character|Your Leader) ", a) {
                    let b2 = format!("{}{b}", &subj[0]);
                    let b2 = match &dur {
                        Some(d) if !b2.ends_with(d.as_str()) => format!("{b2}{d}"),
                        _ => b2,
                    };
                    if let Ok(second) = clauses(&b2) {
                        return Ok(Body::Seq(Box::new(first), Box::new(second)));
                    }
                }
            }
            if let Some(d) = &a_dur {
                if let Ok(second) = clauses(&cap(&format!("{b}{d}"))) {
                    if clauses(&cap(b)).is_err() {
                        return Ok(Body::Seq(Box::new(first), Box::new(second)));
                    }
                }
            }
            let b = b.strip_prefix(", ").unwrap_or(b);
            if let Some(x) = b.strip_prefix("if ") {
                if let Ok(second) = sentence(&format!("If {x}")) {
                    return Ok(Body::Seq(Box::new(first), Box::new(second)));
                }
            }
            let b2 = b.strip_prefix("you may ").map(|x| format!("You may {x}"));
            let second = match b2 {
                Some(x) => sentence(&x),
                None => clauses(&cap(b)),
            };
            if let Ok(second) = second {
                return Ok(Body::Seq(Box::new(first), Box::new(second)));
            }
        }
    }
    Err(Unsupported)
}

fn act(sel: Sel, verb: Verb) -> Body {
    Body::Act(Box::new(Act { sel, verb, opp_chooses: false }))
}

fn until(d: Option<&str>) -> Res<Until> {
    Ok(match d.map(str::trim).unwrap_or("") {
        "during this turn" => Until::Turn,
        "during this battle" => Until::Battle,
        "until the end of your opponent's next End Phase" | "until the end of your opponent's next turn" => Until::NextEnd,
        "until the start of your next turn" => Until::NextTurn,
        "until the end of your next turn" => Until::YourNextEnd,
        _ => return Err(Unsupported),
    })
}

const DUR: &str = r"(?P<d> during this turn| during this battle| until the end of your opponent's next End Phase| until the end of your opponent's next turn| until the start of your next turn| until the end of your next turn)?";

/// One action.
fn clause(c: &str) -> Res<Body> {
    let c = c.trim().trim_end_matches('.');
    if let Ok(b) = parse_body(&format!("{c}.")) {
        if !matches!(b, Body::Seq(..)) {
            return Ok(b);
        }
    }
    clause_gen(c)
}

/// One action, read only as a general action.
fn clause_gen(c: &str) -> Res<Body> {
    let c = c.trim().trim_end_matches('.');
    if let Some(m) = rfull(&format!(r"Your opponent cannot activate (?:a \[Blocker\] Character that has (?P<p>\d+) or (?P<pd>less|more) power|the \[Blocker\] of any Character with a cost of (?P<c>\d+) or less|\[Blocker\]){DUR}"), c) {
        let mut pred = Pred { cats: vec![Category::Character], ..Pred::default() };
        if g(&m, "p").is_some() {
            pred.power.push((cmp_of(g(&m, "pd")), Num::K(gi(&m, "p")), false));
        }
        if g(&m, "c").is_some() {
            pred.cost.push((Cmp::Le, Num::K(gi(&m, "c")), false));
        }
        return Ok(act(Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::All, pred }, Verb::Grant(Kw::NoBlocker, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(r"Rest up to (?:a total of )?(?P<n>\d+) of your opponent's (?:DON!! cards or (?P<np>.+)|Characters or DON!! cards)", c) {
        let pred = match g(&m, "np") {
            Some(x) => np(x)?,
            None => Pred { cats: vec![Category::Character], ..Pred::default() },
        };
        return Ok(Body::Do(Do::RestDonOrChar { n: gu(&m, "n"), pred }));
    }
    let n = |m: &regex::Captures, k: &str| gu(m, k);
    if rfull(r"[Ll]ook at up to 1 card from the top of your or your opponent's Life cards,? and place it at the top or bottom of the Life cards", c).is_some() {
        return Ok(Body::Do(Do::PeekLife));
    }
    if let Some(m) = rfull(r"You cannot play Character cards with a base cost of (?P<n>\d+) or more during this turn", c) {
        return Ok(Body::Do(Do::NoPlayCost(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"[Rr]eturn (?P<s>.+?) to the (?P<pos>top|bottom) of (?:your|the owner's|their) deck(?: in any order)?", c) {
        return Ok(act(sel(&m["s"])?, Verb::ToDeck { bottom: &m["pos"] == "bottom", ends: false }));
    }
    if let Some(m) = rfull(r"[Rr]eturn (?P<s>.+?) to (?:your|the owner's|their) deck", c) {
        return Ok(act(sel(&m["s"])?, Verb::ToDeck { bottom: false, ends: false }));
    }
    if let Some(m) = rfull(r"[Aa]dd up to (?P<n>\d+) additional DON!! cards? (?:from your DON!! deck )?and (?P<how>set (?:it|them) as active|rest (?:it|them))", c) {
        return Ok(Body::T(Tpl::AddDon { n: gu(&m, "n") as u32, rested: m["how"].starts_with("rest") }));
    }
    if rfull(r"[Dd]raw cards equal to the number (?:you returned to your deck|of cards placed|you placed at the bottom of your deck)", c).is_some() {
        return Ok(Body::Do(Do::DrawCount(Num::Counted)));
    }
    if rfull(r"[Tt]rash the same number of cards from the top of your deck as you did from your hand", c).is_some() {
        return Ok(Body::Do(Do::MillCount));
    }
    if let Some(m) = rfull(r"[Pp]lay up to 1 each of (?P<n>\[[^\]]+\](?:, \[[^\]]+\])*,? and \[[^\]]+\]) (?P<q>with .+?) from your (?P<z>hand|trash)", c) {
        return play_each(&crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), &m["q"], &m["z"]);
    }
    if let Some(m) = rfull(r"[Pp]lay (?P<n>up to 1 \[[^\]]+\](?:, up to 1 \[[^\]]+\])*,? and up to 1 \[[^\]]+\]),? (?P<q>with .+?) from your (?P<z>hand|trash)", c) {
        return play_each(&crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), &m["q"], &m["z"]);
    }
    if let Some(m) = rfull(r"Your opponent may return (?P<n>\d+) of their active DON!! cards? to their DON!! deck", c) {
        return Ok(Body::OppMay(Box::new(Body::Do(Do::OppReturnActiveDon(gi(&m, "n") as u32)))));
    }
    if let Some(m) = rfull(r"K\.O\. or rest (?P<s>.+)", c) {
        return Ok(act(sel(&m["s"])?, Verb::KoOrRest));
    }
    if let Some(m) = rfull(&format!(r"(?P<v>Negate the effect of|Give) up to 1 (?:of )?each of your opponent's Leader and Character cards(?P<r>.*?){DUR}"), c) {
        let d = g(&m, "d").unwrap_or("");
        let a = clause_gen(&format!("{} up to 1 of your opponent's Leader{}{d}", &m["v"], &m["r"]))?;
        let b = clause_gen(&format!("{} up to 1 of your opponent's Characters{}{d}", &m["v"], &m["r"]))?;
        return Ok(Body::Seq(Box::new(a), Box::new(b)));
    }
    if let Some(m) = rfull(r"Up to (?P<n>\d+) of your opponent's rested DON!! cards will not become active in your opponent's next Refresh Phase", c) {
        return Ok(Body::Do(Do::OppDonNoRefresh(gi(&m, "n") as u32)));
    }
    if let Some(m) = rfull(&format!(r"Your opponent cannot activate up to (?P<n>\d+) \[Blocker\] Characters? that ha(?:s|ve) (?P<p>\d+) power or (?P<pd>less|more){DUR}"), c) {
        let pred = Pred { cats: vec![Category::Character], power: vec![(cmp_of(g(&m, "pd")), Num::K(gi(&m, "p")), false)], ..Pred::default() };
        let s = Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::UpTo(gu(&m, "n")), pred };
        return Ok(act(s, Verb::Grant(Kw::NoBlocker, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) gains? (?:an additional )?\+(?P<v>\d+) power for each of your (?P<np>.+?){DUR}"), c) {
        if g(&m, "d").is_some() {
            let per = counted(Side::Me, &m["np"])?;
            let s = sel(&m["s"])?;
            return Ok(Body::Seq(Box::new(Body::Do(Do::SetCount(per))), Box::new(act(s, Verb::PowerPer(gi(&m, "v"), until(g(&m, "d"))?)))));
        }
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) gains? (?:an additional )?\+(?P<v>\d+) power{DUR} for (?:each of your|every) (?P<np>.+)"), c) {
        if !rmatch(r"^(?:card trashed|returned Character|Character K\.O\.'d|\d+ .*)$", &m["np"]).is_some() {
            let per = counted(Side::Me, &m["np"])?;
            let s = sel(&m["s"])?;
            return Ok(Body::Seq(Box::new(Body::Do(Do::SetCount(per))), Box::new(act(s, Verb::PowerPer(gi(&m, "v"), until(g(&m, "d"))?)))));
        }
    }
    if let Some(m) = rfull(&format!(r"For every (?P<np>.+?) on your field, give (?P<s>.+?) -(?P<v>\d+) power{DUR}"), c) {
        let per = counted(Side::Me, &m["np"])?;
        let s = sel(&m["s"])?;
        return Ok(Body::Seq(Box::new(Body::Do(Do::SetCount(per))), Box::new(act(s, Verb::PowerPer(-gi(&m, "v"), until(g(&m, "d"))?)))));
    }
    if let Some(m) = rfull(r"[Ss]elect (?P<s>.+?) and play it or add it to the top of your Life cards face-up", c) {
        return Ok(act(sel(&m["s"])?, Verb::PlayOrLife));
    }
    if let Some(m) = rfull(r"[Cc]hoose (?P<n>\d+) cards? from your opponent's hand; your opponent reveals (?:that card|those cards)", c) {
        let s = Sel::Cards { side: Side::Opp, place: Place::Hand, many: Many::Exactly(gu(&m, "n")), pred: Pred::default() };
        return Ok(act(s, Verb::Reveal));
    }
    if let Some(m) = rfull(r"Your opponent trashes (?P<n>\d+) cards? from their hand and reveals their hand", c) {
        let s = Sel::Cards { side: Side::Opp, place: Place::Hand, many: Many::Exactly(gu(&m, "n")), pred: Pred::default() };
        let t = Body::Act(Box::new(Act { sel: s, verb: Verb::Trash, opp_chooses: true }));
        let all = Sel::Cards { side: Side::Opp, place: Place::Hand, many: Many::All, pred: Pred::default() };
        return Ok(Body::Seq(Box::new(t), Box::new(act(all, Verb::Reveal))));
    }
    if rfull(r"Your opponent returns all cards in their hand to their deck and shuffles their deck", c).is_some() {
        let all = Sel::Cards { side: Side::Opp, place: Place::Hand, many: Many::All, pred: Pred::default() };
        return Ok(Body::Seq(Box::new(act(all, Verb::ToDeck { bottom: false, ends: false })), Box::new(Body::Do(Do::ShuffleOpp))));
    }
    if let Some(m) = rfull(r"Your opponent must (?P<rest>place .+)", c) {
        return clause_gen(&format!("Your opponent places {}", m["rest"].trim_start_matches("place ")));
    }
    if let Some(m) = rfull(r"[Ll]ook at all (?:of )?your Life cards; place (?P<n>\d+)(?: cards?)? at the top of your deck and place the rest back in your Life area in any order", c) {
        return Ok(Body::Do(Do::LifeToDeckTop(gu(&m, "n"))));
    }
    if let Some(m) = rfull(&format!(r"This Character gains (?P<a>\[[^\]]+\]), (?P<b>\[[^\]]+\]) or (?P<c>\[[^\]]+\]){DUR}"), c) {
        let d = g(&m, "d").unwrap_or("");
        let mut options = vec![];
        for k in ["a", "b", "c"] {
            options.push((m[k].to_string(), clause_gen(&format!("This Character gains {}{d}", &m[k]))?));
        }
        return Ok(Body::ChooseOne { opponent: false, options });
    }
    if let Some(m) = rfull(&format!(r"[Ss]et the base power of (?P<s>.+?) to (?P<v>\d+){DUR}"), c) {
        let u = if g(&m, "d").is_some() { until(g(&m, "d"))? } else { Until::Turn };
        return Ok(act(sel(&m["s"])?, Verb::BasePower(gi(&m, "v"), u)));
    }
    if let Some(m) = rfull(&format!(r"Your Leader and this Character's base power becomes (?P<v>\d+){DUR}"), c) {
        let a = clause_gen(&format!("Your Leader's base power becomes {}{}", &m["v"], g(&m, "d").unwrap_or("")))?;
        let b = clause_gen(&format!("This Character's base power becomes {}{}", &m["v"], g(&m, "d").unwrap_or("")))?;
        return Ok(Body::Seq(Box::new(a), Box::new(b)));
    }
    if let Some(m) = rfull(r"This Character gains \[(?P<kw>[^\]]+)\]", c) {
        // no duration: for as long as it is on the field
        return Ok(act(Sel::This, Verb::Grant(Kw::printed(&m["kw"]).ok_or(Unsupported)?, Until::Permanent)));
    }
    if rfull(r"[Tt]rash the same number of cards from your hand", c).is_some() {
        return Ok(Body::Do(Do::TrashCount));
    }
    if rfull(r"[Yy]ou cannot set DON!! cards as active using Character effects during this turn", c).is_some() {
        return Ok(Body::Do(Do::NoDonActiveByChar));
    }
    if rfull(r"[Yy]ou cannot attack a Leader during this turn", c).is_some() {
        return Ok(Body::Do(Do::NoLeaderAttack));
    }
    if rfull(r"[Yy]ou cannot play cards from your hand during this turn", c).is_some() {
        return Ok(Body::Do(Do::NoHandPlay));
    }
    if rfull(r"[Yy]ou cannot draw cards using your own effects during this turn", c).is_some() {
        return Ok(Body::Do(Do::NoDraw));
    }
    if rfull(r"[Tt]ake an extra turn after this one", c).is_some() {
        return Ok(Body::Do(Do::ExtraTurn));
    }
    if rfull(r"[Yy]ou win the game", c).is_some() {
        return Ok(Body::Do(Do::WinGame));
    }
    if rfull(r"Your opponent's \[On Play\] effects are negated until the end of your opponent's next turn", c).is_some() {
        return Ok(Body::Do(Do::OppNoOnPlay));
    }
    if let Some(m) = rfull(r"[Yy]our opponent rests (?P<n>\d+) of their active DON!! cards at the start of their next Main Phase", c) {
        return Ok(Body::Do(Do::OppRestDonAtMain(gi(&m, "n") as u32)));
    }
    if rfull(r"[Rr]est any number of your DON!! cards", c).is_some() {
        return Ok(Body::Do(Do::RestDonAny));
    }
    if let Some(m) = rfull(r"K\.O\. up to (?P<n>\d+) of your opponent's Characters with a total power of (?P<b>\d+) or less", c) {
        return Ok(Body::Do(Do::KoTotalPower { n: gu(&m, "n"), budget: gi(&m, "b") }));
    }
    if let Some(m) = rfull(r"K\.O\. up to 1 of your opponent's (?P<a>Characters .+?) or your opponent's (?P<b>Stages .+)", c) {
        let pred = Pred { any_of: vec![np(&m["a"])?, np(&m["b"])?], ..Pred::default() };
        return Ok(act(Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::UpTo(1), pred }, Verb::Ko));
    }
    if let Some(m) = rfull(r"Give up to (?P<n>\d+) (?:total )?of your currently given DON!! cards to 1 of your (?P<np>.+)", c) {
        return Ok(Body::Do(Do::MoveGivenDon { n: gu(&m, "n") as u32, to: np(&m["np"])? }));
    }
    if let Some(m) = rfull(&format!(r"[Gg]ive your opponent's Leader and all of their Characters -(?P<v>\d+) power{DUR}"), c) {
        let u = until(g(&m, "d"))?;
        let all = Pred { cats: vec![Category::Leader, Category::Character], ..Pred::default() };
        return Ok(act(Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::All, pred: all }, Verb::Power(-gi(&m, "v"), u)));
    }
    if let Some(m) = rfull(&format!(r"Give all of your opponent's Characters -(?P<v>\d+) power{DUR} for every DON!! card given to that Character"), c) {
        return Ok(Body::Do(Do::DebuffPerDon(gi(&m, "v"), until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"Select up to 2 of your opponent's Characters, and give 1 Character -(?P<a>\d+) power and the other -(?P<b>\d+) power{DUR}"), c) {
        return Ok(Body::Do(Do::TwoDebuffs(gi(&m, "a"), gi(&m, "b"), until(g(&m, "d"))?)));
    }
    if rfull(r"Up to 1 of your opponent's rested Character or DON!! cards will not become active in your opponent's next Refresh Phase", c).is_some() {
        return Ok(Body::Do(Do::NoRefreshCharOrDon));
    }
    if let Some(m) = rfull(r"[Ss]elect your Leader and 1 (?P<np>Character.*)", c) {
        return Ok(Body::Do(Do::PickLeaderAnd(np(&m["np"])?)));
    }
    if let Some(m) = rfull(r"[Tt]rash 1 of your (?P<np>.+?) at the end of this turn", c) {
        return Ok(Body::Do(Do::LaterTrashOne(np(&m["np"])?)));
    }
    if let Some(m) = rfull(r"[Ss]et all of your (?P<np>.+?) as active at the end of this turn", c) {
        return Ok(Body::Do(Do::LaterSetActiveAll(np(&m["np"])?)));
    }
    if rfull(r"[Pp]lace the rest at the bottom of your deck", c).is_some() {
        return Ok(act(Sel::Rest, Verb::ToDeck { bottom: true, ends: false }));
    }
    if rfull(r"[Tt]rash 1 card from the top of each of your and your opponent's Life cards", c).is_some() {
        let mine = Sel::Cards { side: Side::Me, place: Place::LifeTop(1), many: Many::Exactly(1), pred: Pred::default() };
        let theirs = Sel::Cards { side: Side::Opp, place: Place::LifeTop(1), many: Many::Exactly(1), pred: Pred::default() };
        return Ok(Body::Seq(Box::new(act(mine, Verb::Trash)), Box::new(act(theirs, Verb::Trash))));
    }
    if let Some(m) = rfull(&format!(r"For every DON!! card rested this way, (?P<s>.+?) gains? \+(?P<v>\d+) power{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::PowerPer(gi(&m, "v"), until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) gains? \+(?P<v>\d+) power{DUR} for every (?P<k>\d+) cards placed at the bottom of your deck"), c) {
        let per = Do::SetCount(Num::Per(Box::new(Num::Counted), gi(&m, "k"), 1));
        return Ok(Body::Seq(Box::new(Body::Do(per)), Box::new(act(sel(&m["s"])?, Verb::PowerPer(gi(&m, "v"), until(g(&m, "d"))?)))));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) gains? \+(?P<v>\d+) power{DUR} per 1 cost on the revealed card"), c) {
        return Ok(Body::Seq(Box::new(Body::Do(Do::SetCount(Num::ItCost))), Box::new(act(sel(&m["s"])?, Verb::PowerPer(gi(&m, "v"), until(g(&m, "d"))?)))));
    }
    if rfull(r"[Dd]raw cards equal to the number of cards trashed", c).is_some() {
        return Ok(Body::Do(Do::DrawCount(Num::HandTrashedCount)));
    }
    if rfull(r"[Ss]et this Character or up to 1 of your DON!! cards as active", c).is_some() {
        let a = clause_gen("Set this Character as active")?;
        let b = clause("Set up to 1 of your DON!! cards as active")?;
        return Ok(Body::ChooseOne { opponent: false, options: vec![("Set this Character as active".into(), a), ("Set up to 1 DON!! as active".into(), b)] });
    }
    if rfull(r"[Cc]hoose a cost and reveal 1 card from the top of your opponent's deck", c).is_some() {
        return Ok(Body::Do(Do::GuessCost));
    }
    if rfull(r"[Ll]ook at 1 card from the top of your opponent's deck", c).is_some() {
        return Ok(Body::Do(Do::LookOppTop));
    }
    if let Some(m) = rfull(&format!(r"[Ss]wap the base power of the selected (?:Characters|cards) with each other{DUR}"), c) {
        return Ok(Body::Do(Do::SwapBase(until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"This Character's base power becomes the same as (?P<x>.+?){DUR}"), c) {
        let n = match &m["x"] {
            "your opponent's Leader" | "your opponent's Leader's power" => Num::LeaderPower(Side::Opp),
            "the selected Character's power" => Num::ItPower,
            "the power of your opponent's attacking Leader or Character" => Num::AttackerPower,
            "your Leader's base power" => Num::LeaderBase(Side::Me),
            _ => return Err(Unsupported),
        };
        return Ok(Body::Do(Do::BasePowerFrom(n, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"[Ss]et the (?P<w>power|cost) of (?P<s>.+?) to (?P<v>\d+){DUR}"), c) {
        let u = until(g(&m, "d"))?;
        let verb = if &m["w"] == "power" { Verb::PowerSet(gi(&m, "v"), u) } else { Verb::CostSet(gi(&m, "v"), u) };
        return Ok(act(sel(&m["s"])?, verb));
    }
    if let Some(m) = rfull(r"Give (?:up to )?(?P<n>\d+) (?:of your opponent's (?P<r>rested )?DON!! cards?|DON!! cards? from your opponent's cost area) to (?P<s>.+)", c) {
        let v = Verb::GiveOppDon { n: gu(&m, "n") as u32, rested: g(&m, "r").is_some() };
        return Ok(act(sel(&m["s"])?, v));
    }
    if let Some(m) = rfull(r"Give up to 1 (?P<a>rested DON!! card|DON!! card from its owner's cost area) to its owner's Leader or 1 of their Characters", c) {
        return Ok(Body::Do(Do::GiveDonToOwner { any_area: m["a"].contains("cost area") }));
    }
    if let Some(m) = rfull(&format!(r"[Gg]ive (?P<sg>[+-])(?P<v>\d+) (?P<w>power|cost)(?P<d>{}) to (?P<s>.+)", &DUR[6..DUR.len() - 2]), c) {
        return clause_gen(&format!("Give {} {}{} {}{}", &m["s"], &m["sg"], &m["v"], &m["w"], &m["d"]));
    }
    if let Some(m) = rfull(r"[Aa]ctivate (?:the \[Main\] effect of )?up to 1 (?P<np>.+?) (?P<w>from your hand|in your trash)", c) {
        return Ok(Body::Do(Do::ActivateEvent { trash: m["w"].contains("trash"), pred: np(&m["np"])? }));
    }
    if let Some(m) = rfull(r"[Tt]he next time you play (?:a )?(?P<np>.+?) from your hand during this turn, the cost will be reduced by (?P<n>\d+)", c) {
        let mut pred = np(&m["np"])?;
        for x in pred.cost.iter_mut() {
            x.2 = true;
        }
        return Ok(Body::Do(Do::NextPlayDiscount(pred, gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"Your opponent cannot activate \[Blocker\] (?:when|whenever|if) (?P<s>.+?) attacks during this turn", c) {
        let s = match &m["s"] {
            "the card given these DON!! cards" | "that Leader or Character" => Sel::It,
            x => sel(x)?,
        };
        return Ok(act(s, Verb::Grant(Kw::Unblockable, Until::Turn)));
    }
    if let Some(m) = rfull(r"[Cc]hange the (?:attack target|target of (?:that|the) attack) to (?P<s>.+)", c) {
        let t = &m["s"];
        let s = if let Some(x) = rfull(r"this Leader or to one of your (?P<np>.+)", t) {
            let leader = Pred { cats: vec![Category::Leader], ..Pred::default() };
            let pred = Pred { any_of: vec![leader, np(&x["np"])?], ..Pred::default() };
            Sel::Cards { side: Side::Me, place: Place::Field, many: Many::Exactly(1), pred }
        } else if let Some(x) = rfull(r"your (?P<np>\[.+)", t) {
            Sel::Cards { side: Side::Me, place: Place::Field, many: Many::Exactly(1), pred: np(&x["np"])? }
        } else {
            sel(t)?
        };
        return Ok(act(s, Verb::Redirect));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) gains? (?:an additional )?\+(?P<v>\d+) power{DUR} for every (?:card trashed|returned Character|Character K\.O\.'d)"), c) {
        return Ok(act(sel(&m["s"])?, Verb::PowerPer(gi(&m, "v"), until(g(&m, "d"))?)));
    }
    if rfull(r"[Ss]huffle (?:your deck|it)", c).is_some() {
        return Ok(Body::Do(Do::Shuffle));
    }
    if rfull(r"You cannot play any Character cards(?: on your field)? during this turn", c).is_some() {
        return Ok(Body::T(Tpl::NoCharacterPlay));
    }
    if let Some(m) = rfull(r"Your opponent chooses (?P<n>\d+) cards? from their hand and trashes (?:it|them)", c) {
        let s = Sel::Cards { side: Side::Opp, place: Place::Hand, many: Many::Exactly(gu(&m, "n")), pred: Pred::default() };
        return Ok(Body::Act(Box::new(Act { sel: s, verb: Verb::Trash, opp_chooses: true })));
    }
    if let Some(m) = rfull(r"Your opponent chooses (?P<n>\d+) cards? from your hand; trash (?:that card|those cards)", c) {
        let s = Sel::Cards { side: Side::Me, place: Place::Hand, many: Many::Exactly(gu(&m, "n")), pred: Pred::default() };
        return Ok(Body::Act(Box::new(Act { sel: s, verb: Verb::Trash, opp_chooses: true })));
    }
    if let Some(m) = rfull(r"They (?P<rest>place .+)", c) {
        return clause_gen(&format!("Your opponent places {}", m["rest"].trim_start_matches("place ")));
    }
    if let Some(m) = rfull(&format!(r"None of your (?P<np>.+?) can be K\.O\.'d{DUR}"), c) {
        let mut pred = np(&m["np"])?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        let s = Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred };
        let u = until(g(&m, "d"))?;
        return Ok(Body::Seq(Box::new(act(s.clone(), Verb::Grant(Kw::NoKoInBattle, u))), Box::new(act(s, Verb::Grant(Kw::NoKoByEffects, u)))));
    }
    if let Some(m) = rfull(&format!(r"This Character and up to (?P<n>\d+) of your (?P<np>.+?) gain (?P<rest>.+)"), c) {
        let a = clause_gen(&format!("This Character gains {}", &m["rest"]))?;
        let b = clause_gen(&format!("Up to {} of your {} gains {}", &m["n"], &m["np"], &m["rest"]))?;
        return Ok(Body::Seq(Box::new(a), Box::new(b)));
    }
    if let Some(m) = rfull(r"(?P<v>K\.O\.|[Rr]est|[Rr]eturn|[Pp]lace|[Tt]rash) (?P<a>this \w+|your \w+) and (?P<b>(?:up to \d+|all) .+)", c) {
        if let (Ok(x), Ok(y)) = (clause_gen(&format!("{} {}", &m["v"], &m["a"])), clause_gen(&format!("{} {}", &m["v"], &m["b"]))) {
            return Ok(Body::Seq(Box::new(x), Box::new(y)));
        }
    }
    if let Some(m) = rfull(r"[Dd]raw up to (?P<n>\d+) cards?", c) {
        return Ok(Body::Do(Do::DrawUpTo(n(&m, "n") as u32)));
    }
    if let Some(m) = rfull(r"[Dd]raw cards? so that you have (?P<n>\d+) cards in your hand", c) {
        return Ok(Body::Do(Do::DrawTo(n(&m, "n"))));
    }
    if let Some(m) = rfull(r"[Dd]raw a card for each of your (?P<np>.+)", c) {
        return Ok(Body::Do(Do::DrawCount(counted(Side::Me, &m["np"])?)));
    }
    if let Some(m) = rfull(r"[Tt]rash cards from your hand until you have (?P<n>\d+) cards? in your hand", c) {
        return Ok(Body::Do(Do::TrashTo { n: n(&m, "n"), both: false }));
    }
    if let Some(m) = rfull(r"You and your opponent trash cards from your hands until you each have (?P<n>\d+) cards? in your hands", c) {
        return Ok(Body::Do(Do::TrashTo { n: n(&m, "n"), both: true }));
    }
    if let Some(m) = rfull(r"You take (?P<n>\d+) damage", c) {
        return Ok(Body::Do(Do::TakeDamage(n(&m, "n") as u32)));
    }
    if let Some(m) = rfull(r"[Ll]ook at (?P<n>\d+) cards from the top of your deck and place them at the top of your deck in any order", c) {
        return Ok(Body::Do(Do::ReorderTop(n(&m, "n"))));
    }
    if let Some(m) = rfull(r"[Ll]ook at (?P<n>\d+) cards from the top of your deck and return them to the top or bottom of the deck in any order", c) {
        return Ok(Body::T(Tpl::Scry { n: n(&m, "n") }));
    }
    if let Some(m) = rfull(r"[Ll]ook at all (?:of )?(?P<w>your|your opponent's) Life cards and place them back in (?:your|their) Life area in any order", c) {
        return Ok(Body::Do(Do::ReorderLife { opp: m["w"].contains("opponent") }));
    }
    if rfull(r"[Ss]et all of your DON!! cards as active", c).is_some() {
        return Ok(Body::Do(Do::SetAllDonActive));
    }
    if let Some(m) = rfull(r"[Rr]est (?P<n>\d+) of your DON!! cards", c) {
        return Ok(Body::Do(Do::RestDon { opp: false, n: n(&m, "n") as u32 }));
    }
    if let Some(m) = rfull(r"Add (?:up to )?(?P<n>\d+) DON!! cards? from your DON!! deck and (?P<how>set (?:it|them) as active|rest (?:it|them))", c) {
        return Ok(Body::T(Tpl::AddDon { n: n(&m, "n") as u32, rested: m["how"].starts_with("rest") }));
    }
    if let Some(m) = rfull(r"[Aa]dd (?:up to )?(?P<n>\d+) DON!! cards? from your DON!! deck and set (?:it|them) as active at the end of this turn", c) {
        return Ok(Body::Do(Do::AddDonLater(n(&m, "n") as u32)));
    }
    if let Some(m) = rfull(r"[Aa]dd (?:up to )?(?P<n>\d+) cards? from the top of your deck to the top of your Life cards at the end of this turn", c) {
        return Ok(Body::Do(Do::DeckToLifeLater(n(&m, "n"))));
    }
    if let Some(m) = rfull(r"(?P<v>Trash|Return|Place|Set) (?P<s>.+?)(?P<w> to the owner's hand| at the bottom of the owner's deck| as active)? at the end of this turn", c) {
        let lv = match (&m["v"], g(&m, "w")) {
            ("Trash", None) => LaterVerb::Trash,
            ("Return", Some(_)) => LaterVerb::ToHand,
            ("Place", Some(_)) => LaterVerb::ToDeckBottom,
            ("Set", Some(_)) => LaterVerb::SetActive,
            _ => LaterVerb::Trash,
        };
        // which cards is decided now only for "this Character" or cards already chosen
        if let Ok(s @ (Sel::This | Sel::It)) = sel(&m["s"]) {
            return Ok(act(s, Verb::Later(lv)));
        }
    }
    if let Some(m) = rfull(r"[Rr]eturn (?P<s>.+?) to the owner's hand or (?:place it at )?the bottom of (?:their|the owner's) deck", c) {
        return Ok(act(sel(&m["s"])?, Verb::ToHandOrDeck));
    }
    if let Some(m) = rfull(r"K\.O\. (?P<s>.+?),? or return it to the owner's hand", c) {
        return Ok(act(sel(&m["s"])?, Verb::KoOrToHand));
    }
    if let Some(m) = rfull(&format!(r"None of your (?P<np>.+?) can be K\.O\.'d (?P<how>in battle|by effects|by your opponent's effects){DUR}"), c) {
        let k = match &m["how"] {
            "in battle" => Kw::NoKoInBattle,
            "by effects" => Kw::NoKoByEffects,
            _ => Kw::NoKoByOppEffects,
        };
        let mut pred = np(&m["np"])?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        return Ok(act(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }, Verb::Grant(k, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"Your (?P<np>Characters .+?) cannot be K\.O\.'d in battle{DUR}"), c) {
        let pred = np(&m["np"])?;
        return Ok(act(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }, Verb::Grant(Kw::NoKoInBattle, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) cannot be K\.O\.'d{DUR}"), c) {
        let s = sel(&m["s"])?;
        let u = until(g(&m, "d"))?;
        return Ok(Body::Seq(Box::new(act(s, Verb::Grant(Kw::NoKoInBattle, u))), Box::new(act(Sel::It, Verb::Grant(Kw::NoKoByEffects, u)))));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) cannot activate \[Blocker\]{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::Grant(Kw::NoBlocker, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"[Nn]egate the effects of your opponent's Leader and all of their Characters{DUR}"), c) {
        let u = if g(&m, "d").is_some() { until(g(&m, "d"))? } else { Until::Turn };
        let all = Pred { cats: vec![Category::Leader, Category::Character], ..Pred::default() };
        return Ok(act(Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::All, pred: all }, Verb::Negate(u)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?)'s effect is negated{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::Negate(until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(r"[Tt]urn all of your Life cards face-(?P<w>up|down)", c) {
        return Ok(Body::Do(Do::TurnLife { up: &m["w"] == "up" }));
    }
    if let Some(m) = rfull(r"[Tt]urn (?P<s>.+?) face-(?P<w>up|down)", c) {
        return Ok(act(sel(&m["s"])?, Verb::Face(&m["w"] == "up")));
    }
    if let Some(m) = rfull(r"[Tt]urn all of your Life cards face-(?P<w>up|down)", c) {
        return Ok(Body::Do(Do::TurnLife { up: &m["w"] == "up" }));
    }
    if let Some(m) = rfull(r"[Dd]eal (?P<n>\d+) damage to your opponent", c) {
        return Ok(Body::Do(Do::Damage(n(&m, "n") as u32)));
    }
    if let Some(m) = rfull(r"[Tt]rash cards from the top of your Life cards until you have (?P<n>\d+) Life cards?", c) {
        return Ok(Body::Do(Do::TrashLifeUntil(n(&m, "n"))));
    }
    if let Some(m) = rfull(r"[Ss]et up to (?P<n>\d+) of your DON!! cards as active at the end of this turn", c) {
        return Ok(Body::Do(Do::DonActiveAtEnd(n(&m, "n") as u32)));
    }
    if let Some(m) = rfull(r"[Ll]ook at (?P<n>\d+) cards? from the top of your deck and place them at the top or bottom of (?:your|the) deck in any order", c) {
        return Ok(Body::T(Tpl::Scry { n: n(&m, "n") }));
    }
    // "Your opponent places 3 cards from their trash at the bottom of their deck": they choose
    if let Some(m) = rfull(r"Your opponent (?P<v>trashes|places|returns|adds|rests|K\.O\.'s|plays) (?P<rest>.+)", c) {
        let verb = match &m["v"] {
            "trashes" => "Trash",
            "places" => "Place",
            "returns" => "Return",
            "adds" => "Add",
            "rests" => "Rest",
            "plays" => "Play",
            _ => "K.O.",
        };
        let rest = m["rest"].replace("their ", "your opponent's ").replace("your opponent's opponent's", "your");
        if let Ok(Body::Act(mut a)) = clause_gen(&format!("{verb} {rest}")) {
            a.opp_chooses = true;
            return Ok(Body::Act(a));
        }
    }
    if let Some(m) = rfull(r"(?P<v>K\.O\.|[Rr]est|[Rr]eturn|[Pp]lace|[Tt]rash|[Pp]lay) (?P<a>up to \d+ .+?) and (?P<b>up to \d+ .+)", c) {
        // "K.O. up to 1 of ... and up to 1 of ...": two choices with one action
        let tail = rmatch(r"^.+?(?P<t> to (?:the owner's|its owner's|your|their) hand| at the (?:top|bottom) of .+| from your (?:hand|trash)(?: rested)?)$", &m["b"])
            .map(|t| t["t"].to_string())
            .unwrap_or_default();
        let a_text = format!("{} {}{}", &m["v"], &m["a"], tail);
        if let (Ok(x), Ok(y)) = (clause_gen(&a_text), clause_gen(&format!("{} {}", &m["v"], &m["b"]))) {
            return Ok(Body::Seq(Box::new(x), Box::new(y)));
        }
    }
    if let Some(m) = rfull(r"Your Leader and all of your (?P<rest>Characters gain .+)", c) {
        let leader = clause_gen(&format!("Your Leader gains {}", m["rest"].trim_start_matches("Characters gain ")))?;
        let chars = clause_gen(&format!("All of your {}", &m["rest"]))?;
        return Ok(Body::Seq(Box::new(leader), Box::new(chars)));
    }
    if let Some(m) = rfull(r"Give this (?:Character|Leader) up to (?P<n>\d+) rested DON!! cards?", c) {
        return Ok(act(Sel::This, Verb::GiveDon { n: n(&m, "n") as u32, rested: true }));
    }
    if let Some(m) = rfull(r"Give this Leader or (?P<k>\d+) of your Characters up to (?P<n>\d+) rested DON!! cards?", c) {
        let pred = Pred { cats: vec![Category::Leader, Category::Character], ..Pred::default() };
        let s = Sel::Cards { side: Side::Me, place: Place::Field, many: Many::Exactly(gu(&m, "k")), pred };
        return Ok(act(s, Verb::GiveDon { n: n(&m, "n") as u32, rested: true }));
    }
    if let Some(m) = rfull(r"Give your Leader and (?P<k>\d+|all of your) (?:Characters?|of your Characters) up to (?P<n>\d+) rested DON!! cards? each", c) {
        let k = if m["k"].starts_with("all") { Many::All } else { Many::UpTo(gu(&m, "k")) };
        let leader = Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred: Pred { cats: vec![Category::Leader], ..Pred::default() } };
        let chars = Sel::Cards { side: Side::Me, place: Place::Field, many: k, pred: Pred { cats: vec![Category::Character], ..Pred::default() } };
        let v = Verb::GiveDon { n: n(&m, "n") as u32, rested: true };
        return Ok(Body::Seq(Box::new(act(leader, v)), Box::new(act(chars, v))));
    }
    if let Some(m) = rfull(r"Give up to (?P<n>\d+) rested DON!! cards? to each of (?P<s>your .+)", c) {
        let mut s = sel(&format!("all of {}", &m["s"]))?;
        if let Sel::Cards { many, .. } = &mut s {
            *many = Many::All;
        }
        return Ok(act(s, Verb::GiveDon { n: n(&m, "n") as u32, rested: true }));
    }
    if let Some(m) = rfull(r"Give (?P<s>up to \d+ of your .+?) up to (?P<n>\d+) rested DON!! cards? each", c) {
        return Ok(act(sel(&m["s"])?, Verb::GiveDon { n: n(&m, "n") as u32, rested: true }));
    }
    if let Some(m) = rfull(r"[Dd]raw (?P<n>\d+) cards?", c) {
        return Ok(Body::Do(Do::Draw(n(&m, "n"))));
    }
    if let Some(m) = rfull(r"Your opponent draws (?P<n>\d+) cards?", c) {
        return Ok(Body::Do(Do::OppDraw(n(&m, "n"))));
    }
    if let Some(m) = rfull(r"Your opponent returns (?P<n>\d+) DON!! cards? from their field to their DON!! deck", c) {
        return Ok(Body::Do(Do::ReturnDon { opp: true, n: n(&m, "n") as u32 }));
    }
    if let Some(m) = rfull(r"[Rr]eturn (?P<n>\d+) DON!! cards? from your field to your DON!! deck", c) {
        return Ok(Body::Do(Do::ReturnDon { opp: false, n: n(&m, "n") as u32 }));
    }
    if let Some(m) = rfull(r"Rest up to (?P<n>\d+) of your opponent's DON!! cards?", c) {
        return Ok(Body::Do(Do::RestDon { opp: true, n: n(&m, "n") as u32 }));
    }
    if rfull(r"[Ss]huffle your deck", c).is_some() {
        return Ok(Body::Do(Do::Shuffle));
    }
    if rfull(r"Reveal 1 card from the top of your deck", c).is_some() {
        return Ok(Body::Do(Do::RevealTop));
    }
    if let Some(m) = rfull(r"Trash up to (?P<n>\d+) cards? from the top of your opponent's Life cards", c) {
        return Ok(Body::Optional(Box::new(Body::Do(Do::TrashOppLife(n(&m, "n"))))));
    }
    if rfull(r"You cannot add Life cards to your hand using your own effects during this turn", c).is_some() {
        return Ok(Body::Do(Do::NoLifeToHand));
    }
    if let Some(m) = rfull(r"Give (?:up to )?(?P<n>\d+) (?:of your )?(?P<r>rested |active )?DON!! cards? to (?P<s>.+)", c) {
        let s = sel(&m["s"])?;
        let rested = g(&m, "r").map_or(false, |r| r.starts_with("rested"));
        return Ok(act(s, Verb::GiveDon { n: n(&m, "n") as u32, rested }));
    }
    if let Some(m) = rfull(r"K\.O\. (?P<s>.+)", c) {
        return Ok(act(sel(&m["s"])?, Verb::Ko));
    }
    if let Some(m) = rfull(r"[Rr]est (?P<s>.+)", c) {
        return Ok(act(sel(&m["s"])?, Verb::Rest));
    }
    if let Some(m) = rfull(r"[Ss]et (?P<a>.+?) and (?P<b>(?:up to \d+ of |all of )?your .+?) as active", c) {
        if let (Ok(x), Ok(y)) = (clause(&format!("Set {} as active", &m["a"])), clause(&format!("Set {} as active", &m["b"]))) {
            return Ok(Body::Seq(Box::new(x), Box::new(y)));
        }
    }
    if let Some(m) = rfull(r"Your opponent may (?P<rest>.+)", c) {
        let rest = m["rest"].replace("their ", "your opponent's ");
        let inner = if let Some(x) = rfull(r"add (?P<n>\d+) DON!! cards? from your opponent's DON!! deck and set (?:it|them) as active", &rest) {
            Body::Do(Do::OppAddDon(gu(&x, "n") as u32))
        } else {
            clause_gen(&cap(&rest))?
        };
        return Ok(Body::OppMay(Box::new(inner)));
    }
    if let Some(m) = rfull(r"[Ss]et (?P<s>.+) as active", c) {
        let mut s = sel(&m["s"])?;
        if let Sel::Cards { side, .. } = &mut s {
            if *side == Side::Both {
                *side = Side::Me; // "set up to 1 {X} type Character as active": yours
            }
        }
        return Ok(act(s, Verb::SetActive));
    }
    if let Some(m) = rfull(r"[Rr]eturn (?P<s>.+?) to (?P<w>the owner's|its owner's|their owner's|your|their) hand", c) {
        let mut s = sel(&m["s"])?;
        if let Sel::Cards { side, .. } = &mut s {
            if &m["w"] == "your" && *side == Side::Both {
                *side = Side::Me; // "return 1 Character to your hand": one of yours
            }
        }
        return Ok(act(s, Verb::ToHand));
    }
    if let Some(m) = rfull(
        r"[Pp]lace (?P<s>.+?) at the (?P<pos>top|bottom) of (?:the owner's|its owner's|their owner's|your|their|your opponent's) deck(?: in any order)?",
        c,
    ) {
        return Ok(act(sel(&m["s"])?, Verb::ToDeck { bottom: &m["pos"] == "bottom", ends: false }));
    }
    if let Some(m) = rfull(
        r"[Pp]lace (?P<s>.+?) at the top or bottom of (?:the owner's|its owner's|their owner's|your|their) deck(?: in any order)?",
        c,
    ) {
        return Ok(act(sel(&m["s"])?, Verb::ToDeck { bottom: false, ends: true }));
    }
    if let Some(m) = rfull(
        concat!(
            r"(?:[Pp]lace|[Aa]dd) (?P<s>.+?) (?:at|to) the (?P<pos>top or bottom|top|bottom) of ",
            r"(?:the owner's|its owner's|their owner's|your|their|your opponent's) Life cards(?: face-(?P<fu>up|down))?"
        ),
        c,
    ) {
        let pos = &m["pos"];
        let verb = Verb::ToLife { face_up: g(&m, "fu") == Some("up"), ends: pos.contains(" or "), bottom: pos == "bottom" };
        return Ok(act(sel(&m["s"])?, verb));
    }
    if let Some(m) = rfull(r"[Tt]rash (?P<s>.+)", c) {
        return Ok(act(sel(&m["s"])?, Verb::Trash));
    }
    if let Some(m) = rfull(r"[Aa]dd (?P<s>.+?) to (?:your|the owner's|its owner's|their|your opponent's) hand", c) {
        return Ok(act(sel(&m["s"])?, Verb::ToHand));
    }
    if let Some(m) = rfull(&format!(r"[Gg]ive (?P<s>.+?) (?P<sg>[+-])(?P<v>\d+) (?P<w>power|cost){DUR}"), c) {
        let v = gi(&m, "v") * if &m["sg"] == "-" { -1 } else { 1 };
        let u = until(g(&m, "d"))?;
        let verb = if &m["w"] == "cost" { Verb::Cost(v, u) } else { Verb::Power(v, u) };
        return Ok(act(sel(&m["s"])?, verb));
    }
    if let Some(m) = rfull(&format!(r"[Nn]egate the effects? of (?P<s>.+?){DUR}"), c) {
        let u = if g(&m, "d").is_some() { until(g(&m, "d"))? } else { Until::Turn };
        return Ok(act(sel(&m["s"])?, Verb::Negate(u)));
    }
    if let Some(m) = rfull(r"[Pp]lay (?P<s>.+?)(?P<r> rested)?", c) {
        return Ok(act(sel(&m["s"])?, Verb::Play { rested: g(&m, "r").is_some() }));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) gains? (?:an additional )?\+(?P<v>\d+) cost{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::Cost(gi(&m, "v"), until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) can attack Characters on the turn in which (?:it is|they are) played{DUR}"), c) {
        let u = if g(&m, "d").is_some() { until(g(&m, "d"))? } else { Until::Turn };
        return Ok(act(sel(&m["s"])?, Verb::Grant(Kw::RushCharacter, u)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) cannot be rested{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::Grant(Kw::NoRest, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) gains? (?:an additional )?(?:\[(?P<kw>[^\]]+)\])?(?: and )?(?:\+(?P<v>\d+) power)?{DUR}"), c) {
        let u = until(g(&m, "d"))?;
        let s = sel(&m["s"])?;
        let kw = match g(&m, "kw") {
            Some(k) => Some(Kw::printed(k).ok_or(Unsupported)?),
            None => None,
        };
        return match (kw, g(&m, "v")) {
            (Some(k), None) => Ok(act(s, Verb::Grant(k, u))),
            (None, Some(_)) => Ok(act(s, Verb::Power(gi(&m, "v"), u))),
            (Some(k), Some(_)) => Ok(Body::Seq(
                Box::new(act(s, Verb::Grant(k, u))),
                Box::new(act(Sel::It, Verb::Power(gi(&m, "v"), u))),
            )),
            (None, None) => Err(Unsupported),
        };
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) cannot attack{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::Grant(Kw::CannotAttack, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) cannot be K\.O\.'d (?P<how>in battle|by effects|by your opponent's effects){DUR}"), c) {
        let k = match &m["how"] {
            "in battle" => Kw::NoKoInBattle,
            "by effects" => Kw::NoKoByEffects,
            _ => Kw::NoKoByOppEffects,
        };
        return Ok(act(sel(&m["s"])?, Verb::Grant(k, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) cannot be removed from the field by your opponent's effects{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::Grant(Kw::NoRemoveByOpp, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?) can also attack (?:your opponent's )?active Characters{DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::Grant(Kw::AttackActive, until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(&format!(r"(?P<s>.+?)'s? base power becomes (?P<v>\d+){DUR}"), c) {
        return Ok(act(sel(&m["s"])?, Verb::BasePower(gi(&m, "v"), until(g(&m, "d"))?)));
    }
    if let Some(m) = rfull(r"(?P<s>.+?) will not become active in (?:your|your opponent's|the) next Refresh Phase", c) {
        return Ok(act(sel(&m["s"])?, Verb::NoRefresh));
    }
    if let Some(m) = rfull(r"[Rr]eveal (?P<s>.+? from (?:your hand|your deck|the top of your Life cards))", c) {
        return Ok(act(sel(&m["s"])?, Verb::Reveal));
    }
    if let Some(m) = rfull(r"(?:[Cc]hoose|[Ss]elect) (?P<s>.+)", c) {
        return Ok(act(sel(&m["s"])?, Verb::Pick));
    }
    Err(Unsupported)
}

// ---------------------------------------------------------------- selections

fn lower_first(s: &str) -> String {
    let mut ch = s.chars();
    match ch.next() {
        Some(f) => f.to_lowercase().collect::<String>() + ch.as_str(),
        None => String::new(),
    }
}

/// The cards an action takes.
pub fn sel(text: &str) -> Res<Sel> {
    let t = lower_first(text.trim().trim_end_matches(','));
    if rfull(r"this (?:Character|Leader|card|Stage)(?: card)?", &t).is_some() {
        return Ok(Sel::This);
    }
    if rfull(r"it|them|that (?:Character|card|Leader|Stage)|that Leader or Character|those (?:Characters|cards)|the revealed card|the played Character|the card|such Characters?|the selected (?:Characters?|card)|the chosen Character|the Character played (?:with|by) this effect|the \d+ Character played by this effect|the opponent's Character you battled with|the selected card", &t).is_some() {
        return Ok(Sel::It);
    }
    if let Some(m) = rfull(
        r"your (?P<opp>opponent's )?(?:1 )?(?P<act>active )?(?:\[(?P<n>[^\]]+)\] |(?P<ty>\{[^}]+\}(?: or \{[^}]+\})*) type |(?P<mc>multicolored) |<(?P<at>[^>]+)> attribute )?Leader(?: \[(?P<n2>[^\]]+)\])?",
        &t,
    ) {
        let mut pred = Pred { cats: vec![Category::Leader], ..Pred::default() };
        if let Some(n) = g(&m, "n").or_else(|| g(&m, "n2")) {
            pred.names.push(n.into());
        }
        if let Some(ty) = g(&m, "ty") {
            pred.types = braces(ty);
        }
        if g(&m, "mc").is_some() {
            pred.multicolor = Some(true);
        }
        if g(&m, "act").is_some() {
            pred.rested = Some(false);
        }
        if let Some(a) = g(&m, "at") {
            pred.attrs.push(a.into());
        }
        let side = if g(&m, "opp").is_some() { Side::Opp } else { Side::Me };
        return Ok(Sel::Cards { side, place: Place::Field, many: Many::All, pred });
    }
    if let Some(m) = rfull(r"(?P<all>all )?(?:(?P<u>up to )?(?P<n>\d+) of )?(?P<w>your|your opponent's) (?P<f>face-up |face-down )?Life cards", &t) {
        let many = if m.name("all").is_some() || m.name("n").is_none() {
            Many::All
        } else if g(&m, "u").is_some() {
            Many::UpTo(gu(&m, "n"))
        } else {
            Many::Exactly(gu(&m, "n"))
        };
        let pred = Pred { face_up: g(&m, "f").map(|f| f.starts_with("face-up")), ..Pred::default() };
        let side = if m["w"].contains("opponent") { Side::Opp } else { Side::Me };
        return Ok(Sel::Cards { side, place: Place::Life, many, pred });
    }
    if let Some(m) = rfull(r"all your face-(?P<f>up|down) Life cards", &t) {
        let pred = Pred { face_up: Some(&m["f"] == "up"), ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Life, many: Many::All, pred });
    }
    if let Some(m) = rfull(r"your Leader or (?P<n>\d+) of your Stage cards?", &t) {
        let pred = Pred { cats: vec![Category::Leader, Category::Stage], ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::Exactly(gu(&m, "n")), pred });
    }
    // "your Leader or 1 of your Characters"
    if let Some(m) = rfull(r"your Leader or (?P<u>up to )?(?P<n>\d+) of your (?P<np>.+)", &t) {
        let leader = Pred { cats: vec![Category::Leader], ..Pred::default() };
        let pred = Pred { any_of: vec![leader, np(&m["np"])?], ..Pred::default() };
        let many = if g(&m, "u").is_some() { Many::UpTo(gu(&m, "n")) } else { Many::Exactly(gu(&m, "n")) };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many, pred });
    }

    if let Some(m) = rfull(r"your Leader or 1 (?P<np>\[[^\]]+\])", &t) {
        let pred = Pred { any_of: vec![Pred { cats: vec![Category::Leader], ..Pred::default() }, np(&m["np"])?], ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::Exactly(1), pred });
    }
    if let Some(m) = rfull(r"1 of your (?P<a>\[[^\]]+\]) or your (?P<b>\[[^\]]+\]) Leader", &t) {
        let leader = Pred { cats: vec![Category::Leader], names: crate::compile::find_all(r"\[([^\]]+)\]", &m["b"]), ..Pred::default() };
        let pred = Pred { any_of: vec![leader, np(&m["a"])?], ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::Exactly(1), pred });
    }
    if let Some(m) = rfull(r"this Leader or up to (?P<n>\d+) of your (?P<np>.+)", &t) {
        let pred = Pred { any_of: vec![Pred { cats: vec![Category::Leader], ..Pred::default() }, np(&m["np"])?], ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::UpTo(gu(&m, "n")), pred });
    }
    if let Some(m) = rfull(r#"your Leader with a type including "(?P<x>[^"]+)" or 1 Character with a type including "(?P<y>[^"]+)""#, &t) {
        let a = Pred { cats: vec![Category::Leader], type_inc: vec![m["x"].into()], ..Pred::default() };
        let b = Pred { cats: vec![Category::Character], type_inc: vec![m["y"].into()], ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::Exactly(1), pred: Pred { any_of: vec![a, b], ..Pred::default() } });
    }
    if let Some(m) = rfull(r"your opponent's rested Leader or up to 1 of your opponent's (?P<np>Characters.*)", &t) {
        let a = Pred { cats: vec![Category::Leader], rested: Some(true), ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Opp, place: Place::Field, many: Many::UpTo(1), pred: Pred { any_of: vec![a, np(&m["np"])?], ..Pred::default() } });
    }
    if let Some(m) = rfull(r"all of your (?P<a>\[[^\]]+\]) and (?P<b>\{[^}]+\}) type Characters", &t) {
        let a = Pred { cats: vec![Category::Character], names: crate::compile::find_all(r"\[([^\]]+)\]", &m["a"]), ..Pred::default() };
        let b = Pred { cats: vec![Category::Character], types: braces(&m["b"]), ..Pred::default() };
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred: Pred { any_of: vec![a, b], ..Pred::default() } });
    }
    if let Some(m) = rfull(r"up to 1 of your (?P<a>\[[^\]]+\]) Characters or up to 1 of your (?P<b>Characters .+?), (?P<q>with .+)", &t) {
        let mut a = np(&format!("{} Characters", &m["a"]))?;
        let mut b = np(&m["b"])?;
        let mut r = format!(" {}", &m["q"]);
        let mut shared = Pred::default();
        while !r.is_empty() {
            r = qualifier(&r, &mut shared)?;
        }
        a.power.extend(shared.power.clone());
        b.power.extend(shared.power);
        return Ok(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::UpTo(1), pred: Pred { any_of: vec![a, b], ..Pred::default() } });
    }
    // "1 [X] from your hand with a cost of 3 or more": the place goes last
    let t = match rmatch(r"^(?P<a>.+?) (?P<z>from your (?:hand|trash)) (?P<q>(?:with|that) .+)$", &t) {
        Some(m) => format!("{} {} {}", &m["a"], &m["q"], &m["z"]),
        None => t,
    };
    // where from
    // "up to 1 {X} type Character card on your field": one of yours
    let t = match rfull(r"(?P<n>up to \d+ |\d+ |any number of )(?P<rest>.+) on your field", &t) {
        Some(m) if !m["rest"].starts_with("of ") => {
            let n = m["n"].trim_end_matches("of ");
            format!("{}{}your {}", n, if n.ends_with("number ") { "of " } else { "of " }, &m["rest"])
        }
        _ => t.clone(),
    };
    let mut body = t.clone();
    let mut place = Place::Field;
    let mut side: Option<Side> = None;
    if let Some(m) = rmatch(r"^(?P<b>.+?) from the top or bottom of your Life cards$", &t) {
        body = m["b"].to_string();
        place = Place::LifeEnds;
        side = Some(Side::Me);
    } else if let Some(m) = rmatch(r"^(?P<b>.+?) from the top of (?P<w>your|your opponent's) (?P<z>deck|Life cards)$", &t) {
        body = m["b"].to_string();
        side = Some(if m["w"].contains("opponent") { Side::Opp } else { Side::Me });
        place = if &m["z"] == "deck" { Place::DeckTop(0) } else { Place::LifeTop(0) };
    } else if let Some(m) = rmatch(r"^(?P<b>.+?) from (?P<w>your|your opponent's|their) (?P<z>hand or trash|hand|trash|deck|Life area)$", &t) {
        body = m["b"].to_string();
        side = Some(if m["w"].contains("opponent") { Side::Opp } else { Side::Me });
        place = match &m["z"] {
            "hand or trash" => Place::HandTrash,
            "hand" => Place::Hand,
            "trash" => Place::Trash,
            "deck" => Place::Deck,
            _ => Place::Life,
        };
    } else if let Some(m) = rmatch(r"^all cards in your (?P<z>hand|trash)$", &t) {
        let place = if &m["z"] == "hand" { Place::Hand } else { Place::Trash };
        return Ok(Sel::Cards { side: Side::Me, place, many: Many::All, pred: Pred::default() });
    } else if let Some(m) = rmatch(r"^the top card of your (?P<z>deck|Life cards)$", &t) {
        let place = if &m["z"] == "deck" { Place::DeckTop(1) } else { Place::LifeTop(1) };
        return Ok(Sel::Cards { side: Side::Me, place, many: Many::Exactly(1), pred: Pred::default() });
    }

    if rfull(r"this (?:Character |Stage )?card", &body).is_some() {
        return Ok(Sel::This);
    }
    if place != Place::Field && rmatch(r" or (?:up to )?\d+ (?:cards?|of) ", &body).is_some() {
        return Err(Unsupported); // "1 of your Characters or 1 card from your hand": two places
    }
    // how many
    let (many, rest) = if let Some(m) = rmatch(r"^any number of ", &body) {
        (Many::UpTo(60), body[end(&m)..].to_string())
    } else if let Some(m) = rmatch(r"^up to (?:a total of )?(?P<n>\d+) (?:of )?", &body) {
        (Many::UpTo(gu(&m, "n")), body[end(&m)..].to_string())
    } else if let Some(m) = rmatch(r"^all (?:of )?", &body) {
        (Many::All, body[end(&m)..].to_string())
    } else if let Some(m) = rmatch(r"^(?P<n>\d+) (?:of )?", &body) {
        (Many::Exactly(gu(&m, "n")), body[end(&m)..].to_string())
    } else {
        return Err(Unsupported);
    };
    if let Place::DeckTop(_) | Place::LifeTop(_) = place {
        let n = match many {
            Many::UpTo(n) | Many::Exactly(n) => n,
            Many::All => return Err(Unsupported),
        };
        place = if let Place::DeckTop(_) = place { Place::DeckTop(n) } else { Place::LifeTop(n) };
    }

    // whose
    let (who, rest) = if let Some(r) = rest.strip_prefix("your opponent's ") {
        (Some(Side::Opp), r.to_string())
    } else if let Some(r) = rest.strip_prefix("your ") {
        (Some(Side::Me), r.to_string())
    } else {
        (None, rest)
    };
    let side = match (side, who) {
        (Some(s), _) => s,
        (None, Some(s)) => s,
        (None, None) => Side::Both,
    };
    let pred = np(&rest)?;
    if place == Place::Field && side == Side::Both && pred.cats.is_empty() {
        return Err(Unsupported);
    }
    Ok(Sel::Cards { side, place, many, pred })
}

fn braces(s: &str) -> Vec<String> {
    crate::compile::find_all(r"\{([^}]+)\}", s)
}

/// A noun phrase: what the cards must be ("A or B" too).
pub fn np(text: &str) -> Res<Pred> {
    np_only(text).or_else(|e| either(text).ok_or(e))
}

fn np_only(text: &str) -> Res<Pred> {
    let m = rmatch(
        &format!(
            concat!(
                r"^(?:(?P<st>rested|active) )?(?:(?P<col>(?:{c})(?:(?: or |/)(?:{c}))*) )?(?:(?P<mc>multicolored) )?",
                r"(?:(?P<pc>\d+) cost )?(?:(?P<tyo>\{{[^}}]+\}}) type or <(?P<ao>[^>]+)> attribute )?(?:(?P<ty>\{{[^}}]+\}}(?:(?:,? or |, )\{{[^}}]+\}})*) type )?(?:<(?P<attr>[^>]+)> attribute )?(?:(?P<st2>rested|active) )?",
                r"(?P<names>\[[^\]]+\](?:,? or \[[^\]]+\]|,? and \[[^\]]+\]|, \[[^\]]+\])*)?\s?",
                r"(?P<noun>Leader or Character cards?|Leader or Characters?|Character or Stage cards?|Characters? or Stages?|",
                r"Character or Event cards?|Event or Stage cards?|Leader or Stage cards?|Leader and Character cards|Leaders and Characters|Leaders or Characters|Character cards?|Characters?|Leaders?|Stage cards?|Stages?|",
                r"Event cards?|Events?|cards?)?"
            ),
            c = COLORS
        ),
        text.trim(),
    )
    .ok_or(Unsupported)?;
    let mut p = Pred::default();
    if g(&m, "names").is_none() && g(&m, "noun").is_none() {
        return Err(Unsupported);
    }
    if let Some(st) = g(&m, "st") {
        p.rested = Some(st == "rested");
    }
    if let Some(col) = g(&m, "col") {
        p.colors = crate::compile::find_all(&format!(r"({COLORS})"), col);
    }
    if g(&m, "mc").is_some() {
        p.multicolor = Some(true);
    }
    if let Some(ty) = g(&m, "ty") {
        p.types = braces(ty);
    }
    if let (Some(ty), Some(a)) = (g(&m, "tyo"), g(&m, "ao")) {
        // "{Muggy Kingdom} type or <Slash> attribute Character card": either
        p.any_of = vec![Pred { types: braces(ty), ..Pred::default() }, Pred { attrs: vec![a.into()], ..Pred::default() }];
    }
    if let Some(st) = g(&m, "st2") {
        p.rested = Some(st == "rested");
    }
    if let Some(c) = g(&m, "pc") {
        p.cost.push((Cmp::Eq, Num::K(c.parse().unwrap()), false));
    }
    if let Some(a) = g(&m, "attr") {
        p.attrs.push(a.into());
    }
    if let Some(n) = g(&m, "names") {
        p.names = crate::compile::find_all(r"\[([^\]]+)\]", n);
    }
    let noun = g(&m, "noun").unwrap_or("");
    p.cats = match noun {
        _ if noun.starts_with("Leader or Character") || noun.starts_with("Leader and Character") || noun.starts_with("Leaders") && noun.len() > 8 => {
            vec![Category::Leader, Category::Character]
        }
        _ if noun.starts_with("Character or Stage") || noun.starts_with("Characters or Stage") => {
            vec![Category::Character, Category::Stage]
        }
        _ if noun.starts_with("Character or Event") => vec![Category::Character, Category::Event],
        _ if noun.starts_with("Event or Stage") => vec![Category::Event, Category::Stage],
        _ if noun.starts_with("Leader or Stage") => vec![Category::Leader, Category::Stage],
        _ if noun.starts_with("Character") => vec![Category::Character],
        _ if noun.starts_with("Leader") => vec![Category::Leader],
        _ if noun.starts_with("Stage") => vec![Category::Stage],
        _ if noun.starts_with("Event") => vec![Category::Event],
        _ => vec![],
    };
    if noun.is_empty() && !p.names.is_empty() {
        p.cats = vec![];
    }
    let mut rest = text.trim()[end(&m)..].to_string();
    if !rest.is_empty() && !rest.starts_with(' ') {
        rest = format!(" {rest}");
    }
    while !rest.is_empty() {
        rest = qualifier(&rest, &mut p)?;
    }
    Ok(p)
}

const NUM: &str = concat!(
    r"\d+|the (?:total )?number of (?:your opponent's|your) (?:Life cards|rested DON!! cards|DON!! cards)",
    r"|the total of your and your opponent's Life cards|the total number of your and your opponent's Life cards",
    r"|the number of DON!! cards on (?:your|your opponent's) field|the number of cards in (?:your|your opponent's) (?:hand|trash)"
    , r"|your number of Life cards|the number of DON!! cards on your opponent's field"
);

fn num(s: &str) -> Res<Num> {
    let s = s.trim();
    if let Ok(k) = s.parse::<i32>() {
        return Ok(Num::K(k));
    }
    let side = if s.contains("your and your opponent's") {
        Side::Both
    } else if s.contains("opponent's") {
        Side::Opp
    } else {
        Side::Me
    };
    let s = if s == "your number of Life cards" { "the number of your Life cards" } else { s };
    Ok(if s.ends_with("Life cards") {
        Num::Life(side)
    } else if s.contains("rested DON!!") {
        Num::DonRested(side)
    } else if s.contains("DON!!") {
        Num::DonField(side)
    } else if s.ends_with("hand") {
        Num::Hand(side)
    } else if s.ends_with("trash") {
        Num::Trash(side)
    } else {
        return Err(Unsupported);
    })
}

fn cmp_of(d: Option<&str>) -> Cmp {
    match d {
        Some("less") => Cmp::Le,
        Some("more") => Cmp::Ge,
        _ => Cmp::Eq,
    }
}

/// One qualifier at the start of `rest` (which starts with a space); returns what's left.
fn qualifier(rest: &str, p: &mut Pred) -> Res<String> {
    let w = r"^ (?:and with|with|and|that has|that have)";
    if let Some(m) = rmatch(&format!(r"{w} a (?P<b>base )?cost of (?P<a>\d+) to (?P<z>\d+)"), rest) {
        let base = g(&m, "b").is_some();
        p.cost.push((Cmp::Ge, Num::K(gi(&m, "a")), base));
        p.cost.push((Cmp::Le, Num::K(gi(&m, "z")), base));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} a (?P<b>base )?cost of (?P<n>\d+)(?: or (?P<d>less|more))?"), rest) {
        p.cost.push((cmp_of(g(&m, "d")), Num::K(gi(&m, "n")), g(&m, "b").is_some()));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} a (?P<b>base )?cost equal to or (?P<d>less|more) than (?P<x>{NUM})"), rest) {
        p.cost.push((cmp_of(g(&m, "d")), num(&m["x"])?, g(&m, "b").is_some()));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} (?P<a>\d+) to (?P<z>\d+) (?P<b>base )?power"), rest) {
        let base = g(&m, "b").is_some();
        p.power.push((Cmp::Ge, Num::K(gi(&m, "a")), base));
        p.power.push((Cmp::Le, Num::K(gi(&m, "z")), base));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} (?P<n>\d+) or (?P<d>less|more) (?P<b>base )?power"), rest) {
        p.power.push((cmp_of(g(&m, "d")), Num::K(gi(&m, "n")), g(&m, "b").is_some()));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} (?P<n>\d+) (?P<b>base )?power(?: or (?P<d>less|more))?"), rest) {
        p.power.push((cmp_of(g(&m, "d")), Num::K(gi(&m, "n")), g(&m, "b").is_some()));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} (?P<b>base )?power equal to or (?P<d>less|more) than (?P<x>{NUM})"), rest) {
        p.power.push((cmp_of(g(&m, "d")), num(&m["x"])?, g(&m, "b").is_some()));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} a (?P<b>base )?power of (?P<n>\d+)(?: or (?P<d>less|more))?"), rest) {
        p.power.push((cmp_of(g(&m, "d")), Num::K(gi(&m, "n")), g(&m, "b").is_some()));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} a cost (?P<n>\d+)(?: or (?P<d>less|more))?"), rest) {
        p.cost.push((cmp_of(g(&m, "d")), Num::K(gi(&m, "n")), false));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ (?:that has|that have|with) (?P<n>\d+) or more DON!! cards given", rest) {
        p.don_min = Some(gi(&m, "n") as u32);
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ (?:other than|except) this (?:Character|card|Leader|Stage)", rest) {
        p.not_this = true;
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ other than (?P<n>\[[^\]]+\](?: or \[[^\]]+\])*)", rest) {
        p.not_names.extend(crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} (?:a )?\[Trigger\]"), rest) {
        p.trigger = Some(true);
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ without a \[Trigger\]", rest) {
        p.trigger = Some(false);
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} the <(?P<a>[^>]+)>(?: or <(?P<b>[^>]+)>)? attribute"), rest) {
        p.attrs.push(m["a"].to_string());
        if let Some(b) = g(&m, "b") {
            p.attrs.push(b.into());
        }
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r#"{w} a type including "(?P<x>[^"]+)""#), rest) {
        p.type_inc.push(m["x"].to_string());
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"{w} (?:the )?(?P<ty>\{{[^}}]+\}}(?: or \{{[^}}]+\}})*) type"), rest) {
        p.types.extend(braces(&m["ty"]));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ (?:and |with )?different card names", rest) {
        p.distinct_names = true;
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ that is either (?P<n>\[[^\]]+\]) or has the <(?P<a>[^>]+)> attribute", rest) {
        let named = Pred { names: crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), ..Pred::default() };
        let attr = Pred { attrs: vec![m["a"].to_string()], ..Pred::default() };
        p.any_of = vec![named, attr];
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ that is a different color than the returned Character", rest) {
        p.not_color_of_it = true;
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ and the same card name as the trashed card", rest) {
        p.same_name_as_it = true;
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(&format!(r"^ that is equal to or less than (?P<x>{NUM})"), rest) {
        p.cost.push((Cmp::Le, num(&m["x"])?, false));
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ without the <(?P<a>[^>]+)> attribute", rest) {
        p.not_attrs.push(m["a"].to_string());
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ without an? \[(?P<t>[^\]]+)\] effect", rest) {
        p.without_timing = Some(Timing::from_tag(&m["t"]).ok_or(Unsupported)?);
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ (?:with|and) no base effect", rest) {
        p.vanilla = Some(true);
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ (?:that (?:is|are) rested|in rested state)", rest) {
        p.rested = Some(true);
        return Ok(rest[end(&m)..].to_string());
    }
    if let Some(m) = rmatch(r"^ with (?:a|any) DON!! cards? given", rest) {
        p.has_don = Some(true);
        return Ok(rest[end(&m)..].to_string());
    }
    Err(Unsupported)
}

// ---------------------------------------------------------------- conditions

fn side_of(who: &str) -> Res<Side> {
    Ok(match who {
        "you" | "You" => Side::Me,
        "your opponent" | "Your opponent" => Side::Opp,
        _ => return Err(Unsupported),
    })
}

/// A counted thing after "you have N or more": "Life cards", "cards in your hand", "rested
/// Characters", "{X} type Characters", "Events in your trash", "DON!! cards on your field", ...
fn counted(side: Side, what: &str) -> Res<Num> {
    let w = what.trim();
    if rfull(r"Life cards?", w).is_some() {
        return Ok(Num::Life(side));
    }
    if rfull(r"cards? in (?:your|their) hand", w).is_some() {
        return Ok(Num::Hand(side));
    }
    if rfull(r"cards? in (?:your|their) trash", w).is_some() {
        return Ok(Num::Trash(side));
    }
    if rfull(r"cards? in (?:your|their) deck", w).is_some() {
        return Ok(Num::Deck(side));
    }
    if rfull(r"DON!! cards? on (?:your|their) field", w).is_some() {
        return Ok(Num::DonField(side));
    }
    if rfull(r"(?:given DON!! cards?|DON!! cards? given)", w).is_some() {
        return Ok(Num::DonGiven(side));
    }
    if rfull(r"rested cards?", w).is_some() {
        // Leader, Characters, Stage and DON!! (rested cards include DON!!)
        let pred = Pred { rested: Some(true), ..Pred::default() };
        let field = Num::Count(Box::new(Sel::Cards { side, place: Place::Field, many: Many::All, pred }));
        return Ok(Num::Sum(Box::new(field), Box::new(Num::DonRested(side))));
    }
    if let Some(m) = rfull(r"(?P<n>\[[^\]]+\]) in (?:your|their) trash", w) {
        let pred = Pred { names: crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), ..Pred::default() };
        return Ok(Num::Count(Box::new(Sel::Cards { side, place: Place::Trash, many: Many::All, pred })));
    }
    if rfull(r"rested DON!! cards?", w).is_some() {
        return Ok(Num::DonRested(side));
    }
    if rfull(r"active DON!! cards?", w).is_some() {
        return Ok(Num::DonActive(side));
    }
    if let Some(m) = rfull(r"(?P<np>.+?) in (?:your|their) trash", w) {
        let pred = np(&m["np"])?;
        return Ok(Num::Count(Box::new(Sel::Cards { side, place: Place::Trash, many: Many::All, pred })));
    }
    if let Some(m) = rfull(r"(?P<np>.+?) in (?:your|their) hand", w) {
        let pred = np(&m["np"])?;
        return Ok(Num::Count(Box::new(Sel::Cards { side, place: Place::Hand, many: Many::All, pred })));
    }
    let pred = np(w.strip_suffix(" on your field").unwrap_or(w))?;
    if pred.cats.is_empty() && pred.names.is_empty() && pred.types.is_empty() {
        return Err(Unsupported);
    }
    if pred.cats.is_empty() && pred.names.is_empty() {
        // "{Straw Hat Crew} type card on your field": Leader, Characters and Stage
        return Ok(Num::Count(Box::new(Sel::Cards { side, place: Place::Field, many: Many::All, pred })));
    }
    let mut pred = pred;
    if pred.cats.is_empty() {
        pred.cats = vec![Category::Character];
    }
    Ok(Num::Count(Box::new(Sel::Cards { side, place: Place::Field, many: Many::All, pred })))
}

pub fn cond2(clause: &str) -> Res<Q> {
    let c = clause.trim();
    if let Some(x) = cond_part(c) {
        return Ok(Q::Legacy(x));
    }
    if let Ok(q) = cond_one(c) {
        return Ok(q);
    }
    // "A and B", "A or B" (each part a whole condition)
    for (sep, and) in [(" and ", true), (", and ", true), (", ", true), (" or ", false)] {
        for (i, _) in c.match_indices(sep) {
            let left = &c[..i];
            let right = c[i + sep.len()..].to_string();
            // "your Leader has the {X} type and is active", "you have 2 or less Life cards and a Character ..."
            let starts_verb = rmatch(r"^(?:is|has|have) ", &right).is_some();
            let right_full = match (rmatch(r"^(?:your opponent's Leader|your Leader|this Character) (?:is|has) ", left), starts_verb) {
                (Some(subj), false) => format!("{}{right}", &subj[0]),
                _ => match rmatch(r"^(?:your opponent's Leader|your Leader|this Character|you have|your opponent has) ", left) {
                    Some(subj) if starts_verb && !subj[0].ends_with("have ") && !subj[0].ends_with("has ") => format!("{}{right}", &subj[0]),
                    Some(subj) if !starts_verb => format!("{}{right}", &subj[0]),
                    _ => right.clone(),
                },
            };
            let b = cond2(&right).or_else(|_| cond2(&right_full));
            if let (Ok(a), Ok(b)) = (cond2(left), b) {
                return Ok(if and { Q::And(vec![a, b]) } else { Q::Or(vec![a, b]) });
            }
        }
    }
    Err(Unsupported)
}

fn cond_one(c: &str) -> Res<Q> {
    if rfull(r"it is your second turn or later", c).is_some() {
        return Ok(Q::Cmp(Num::MyTurns, Cmp::Ge, Num::K(2)));
    }
    if rfull(r"it is your turn", c).is_some() {
        return Ok(Q::YourTurn);
    }
    if rfull(r"it is your opponent's turn", c).is_some() {
        return Ok(Q::OppTurn);
    }
    let leader_side = |s: Option<&str>| if s.is_some() { Side::Opp } else { Side::Me };
    if let Some(m) = rfull(r"your (?P<o>opponent's )?Leader is multicolored", c) {
        return Ok(Q::Leader(leader_side(g(&m, "o")), Pred { multicolor: Some(true), ..Pred::default() }));
    }
    if let Some(m) = rfull(r"your (?P<o>opponent's )?Leader has the (?P<ty>\{[^}]+\}(?: or \{[^}]+\})*) type", c) {
        return Ok(Q::Leader(leader_side(g(&m, "o")), Pred { types: braces(&m["ty"]), ..Pred::default() }));
    }
    if let Some(m) = rfull(r"your (?P<o>opponent's )?Leader is (?P<n>\[[^\]]+\](?:,? or \[[^\]]+\]|, \[[^\]]+\])*)", c) {
        let names = crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]);
        return Ok(Q::Leader(leader_side(g(&m, "o")), Pred { names, ..Pred::default() }));
    }
    if let Some(m) = rfull(r#"your (?P<o>opponent's )?Leader(?:'s type includes| has a type including) "(?P<x>[^"]+)""#, c) {
        return Ok(Q::Leader(leader_side(g(&m, "o")), Pred { type_inc: vec![m["x"].into()], ..Pred::default() }));
    }
    if let Some(m) = rfull(r"your (?P<o>opponent's )?Leader (?:is|has) (?P<q>.+)", c) {
        let mut p = Pred::default();
        let mut rest = format!(" with {}", m["q"].trim_start_matches("a ").trim_start_matches("an "));
        if &m["q"] == "rested" || &m["q"] == "active" {
            p.rested = Some(&m["q"] == "rested");
            rest.clear();
        }
        while !rest.is_empty() {
            rest = qualifier(&rest, &mut p)?;
        }
        return Ok(Q::Leader(leader_side(g(&m, "o")), p));
    }
    if let Some(m) = rfull(r"this (?:Character|Leader|card|Stage) (?:is|has) (?P<q>.+)", c) {
        let mut p = Pred::default();
        let q = &m["q"];
        if q == "rested" || q == "active" {
            p.rested = Some(q == "rested");
            return Ok(Q::This(p));
        }
        if let Some(x) = rfull(r"(?P<n>\d+) or more DON!! cards given|(?:a|any) DON!! cards? given", q) {
            let n = g(&x, "n").map_or(1, |v| v.parse().unwrap());
            return Ok(Q::Cmp(Num::ThisDon, Cmp::Ge, Num::K(n)));
        }
        let mut rest = format!(" with {q}");
        while !rest.is_empty() {
            rest = qualifier(&rest, &mut p)?;
        }
        return Ok(Q::This(p));
    }
    if rfull(r"you have 0 or 3 or more DON!! cards on your field", c).is_some() {
        return Ok(Q::Or(vec![Q::Cmp(Num::DonField(Side::Me), Cmp::Eq, Num::K(0)), Q::Cmp(Num::DonField(Side::Me), Cmp::Ge, Num::K(3))]));
    }
    if let Some(m) = rfull(r"you do not have (?P<n>\d+) (?P<w>.+)", c) {
        return Ok(Q::Cmp(counted(Side::Me, &m["w"])?, Cmp::Lt, Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"you have activated an Event with a base cost of (?P<n>\d+) or more during this turn", c) {
        return Ok(Q::EventCostAtLeast(gi(&m, "n")));
    }
    if rfull(r"the chosen Character has a cost equal to the number of DON!! cards given to it", c).is_some() {
        return Ok(Q::It(Pred { cost: vec![(Cmp::Eq, Num::ItDon, false)], ..Pred::default() }));
    }
    if let Some(m) = rfull(r"you have (?P<n>\d+) (?P<np>.+?) with different card names", c) {
        let s = Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred: np(&m["np"])? };
        return Ok(Q::Cmp(Num::DistinctNames(Box::new(s)), Cmp::Ge, Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"you have (?P<n>\[[^\]]+\]) (?P<q>with .+?) on your field", c) {
        let mut pred = Pred { cats: vec![Category::Character], names: crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), ..Pred::default() };
        let mut r = format!(" {}", &m["q"]);
        while !r.is_empty() {
            r = qualifier(&r, &mut pred)?;
        }
        return Ok(Q::exists(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }));
    }
    if let Some(m) = rfull(r"there are no other (?P<n>\[[^\]]+\]) cards", c) {
        let pred = Pred { names: crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]), not_this: true, ..Pred::default() };
        return Ok(Q::Not(Box::new(Q::exists(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }))));
    }
    if let Some(m) = rfull(r"you have no other (?P<np>\[[^\]]+\] with .+)", c) {
        let mut pred = np(&m["np"])?;
        pred.not_this = true;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        return Ok(Q::Not(Box::new(Q::exists(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }))));
    }
    if let Some(m) = rfull(r"you have (?P<a>\[[^\]]+\]) and (?P<b>\[[^\]]+\]) Characters (?P<q>with .+)", c) {
        let mut v = vec![];
        for k in ["a", "b"] {
            let mut pred = Pred { cats: vec![Category::Character], names: crate::compile::find_all(r"\[([^\]]+)\]", &m[k]), ..Pred::default() };
            let mut r = format!(" {}", &m["q"]);
            while !r.is_empty() {
                r = qualifier(&r, &mut pred)?;
            }
            v.push(Q::exists(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }));
        }
        return Ok(Q::And(v));
    }
    if rfull(r"the revealed card has the chosen cost", c).is_some() {
        return Ok(Q::GuessHit);
    }
    if let Some(m) = rfull(r"(?:that card|the revealed card|it|that Character|the trashed card|the chosen Character|the selected Character) (?:is|has) (?P<q>.+)", c) {
        let q = &m["q"];
        if let Ok(p) = np(q.trim_start_matches("a ").trim_start_matches("an ")) {
            return Ok(Q::It(p));
        }
        let mut p = Pred::default();
        let mut rest = format!(" with {q}");
        while !rest.is_empty() {
            rest = qualifier(&rest, &mut p)?;
        }
        return Ok(Q::It(p));
    }
    if let Some(m) = rfull(r#"(?:that card's|the revealed card's) type includes "(?P<x>[^"]+)""#, c) {
        return Ok(Q::It(Pred { type_inc: vec![m["x"].into()], ..Pred::default() }));
    }
    if let Some(m) = rfull(r"you have (?P<a>\[[^\]]+\]) and (?P<b>\[[^\]]+\])(?P<w> in your trash)?", c) {
        let w = g(&m, "w").unwrap_or("");
        return Ok(Q::And(vec![cond2(&format!("you have {}{w}", &m["a"]))?, cond2(&format!("you have {}{w}", &m["b"]))?]));
    }
    if let Some(m) = rfull(r"the number of your Life cards is (?P<d>equal to or less than|equal to or more than|less than|more than) the number of your opponent's Life cards", c) {
        let cmp = match &m["d"] {
            "equal to or less than" => Cmp::Le,
            "equal to or more than" => Cmp::Ge,
            "less than" => Cmp::Lt,
            _ => Cmp::Gt,
        };
        return Ok(Q::Cmp(Num::Life(Side::Me), cmp, Num::Life(Side::Opp)));
    }
    if let Some(m) = rfull(r"you have no other (?P<n>\[[^\]]+\]) Characters?", c) {
        let names = crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]);
        let pred = Pred { cats: vec![Category::Character], names, not_this: true, ..Pred::default() };
        return Ok(Q::Not(Box::new(Q::exists(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }))));
    }
    if let Some(m) = rfull(r"your Leader has the (?P<ty>\{[^}]+\}) type or is (?P<n>\[[^\]]+\])", c) {
        return Ok(Q::Or(vec![cond2(&format!("your Leader has the {} type", &m["ty"]))?, cond2(&format!("your Leader is {}", &m["n"]))?]));
    }
    if let Some(m) = rfull(r"(?P<s>there is|your opponent has|you have) a Character (?P<a>with .+?) or (?P<b>with .+)", c) {
        return Ok(Q::Or(vec![cond2(&format!("{} a Character {}", &m["s"], &m["a"]))?, cond2(&format!("{} a Character {}", &m["s"], &m["b"]))?]));
    }
    if let Some(m) = rfull(r"your Leader's colors include (?P<c>red|green|blue|purple|black|yellow)", c) {
        return Ok(Q::Leader(Side::Me, Pred { colors: vec![m["c"].into()], ..Pred::default() }));
    }
    if let Some(m) = rfull(r#"your Leader's card name includes "(?P<x>[^"]+)""#, c) {
        return Ok(Q::Leader(Side::Me, Pred { name_inc: Some(m["x"].into()), ..Pred::default() }));
    }
    if rfull(r"(?:this Character battles|this Character is battling) your opponent's Character", c).is_some() {
        return Ok(Q::BattlesChar);
    }
    if let Some(m) = rfull(r"you have (?P<n>\[[^\]]+\]) in your trash", c) {
        return Ok(Q::Cmp(counted(Side::Me, &format!("{} in your trash", &m["n"]))?, Cmp::Ge, Num::K(1)));
    }
    if let Some(m) = rfull(r"you have a total of (?P<n>\d+) or (?P<d>less|more) cards in your Life area and hand", c) {
        return Ok(Q::Cmp(Num::Sum(Box::new(Num::Life(Side::Me)), Box::new(Num::Hand(Side::Me))), cmp_of(g(&m, "d")), Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r#"you only have (?:(?P<ty>\{[^}]+\}) type Characters|Characters with a type including "(?P<x>[^"]+)")"#, c) {
        let mut pred = Pred { cats: vec![Category::Character], ..Pred::default() };
        if let Some(t) = g(&m, "ty") {
            pred.not_types = braces(t);
        }
        if let Some(x) = g(&m, "x") {
            pred.not_type_inc = vec![x.into()];
        }
        return Ok(Q::Not(Box::new(Q::exists(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }))));
    }
    if let Some(m) = rfull(r"your deck has (?P<n>\d+) cards?", c) {
        return Ok(Q::Cmp(Num::Deck(Side::Me), Cmp::Eq, Num::K(gi(&m, "n"))));
    }
    if rfull(r"it is your second turn or later", c).is_some() {
        return Ok(Q::Cmp(Num::MyTurns, Cmp::Ge, Num::K(2)));
    }
    if let Some(m) = rfull(r"the number of cards in your hand is at least (?P<n>\d+) less than the number in your opponent's hand", c) {
        return Ok(Q::Cmp(Num::Sub(Box::new(Num::Hand(Side::Opp)), Box::new(Num::Hand(Side::Me))), Cmp::Ge, Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"the number of your Characters is at least (?P<n>\d+) less than the number of your opponent's Characters", c) {
        let ch = |s| counted(s, "Characters");
        return Ok(Q::Cmp(Num::Sub(Box::new(ch(Side::Opp)?), Box::new(ch(Side::Me)?)), Cmp::Ge, Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"the total cost of your Characters is (?P<n>\d+) or (?P<d>less|more)", c) {
        return Ok(Q::Cmp(Num::TotalCost(Side::Me), cmp_of(g(&m, "d")), Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"there are (?P<n>\d+) or more (?P<w>Characters.*)", c) {
        return Ok(Q::Cmp(counted(Side::Both, &m["w"])?, Cmp::Ge, Num::K(gi(&m, "n"))));
    }
    if rfull(r"all of your DON!! cards are rested", c).is_some() {
        return Ok(Q::Cmp(Num::DonActive(Side::Me), Cmp::Eq, Num::K(0)));
    }
    if let Some(m) = rfull(r"either you or your opponent has (?P<rest>.+)", c) {
        let rest = m["rest"].replace("on the field", "on your field");
        return Ok(Q::Or(vec![cond2(&format!("you have {rest}"))?, cond2(&format!("your opponent has {rest}"))?]));
    }
    if let Some(m) = rfull(r"(?P<w>your opponent's|your) Character has been K\.O\.'d during this turn", c) {
        return Ok(Q::KoThisTurn(if m["w"].contains("opponent") { Side::Opp } else { Side::Me }));
    }
    if rfull(r"it is your turn", c).is_some() {
        return Ok(Q::YourTurn);
    }
    if rfull(r"it is your opponent's turn", c).is_some() {
        return Ok(Q::OppTurn);
    }
    // comparisons between players
    if let Some(m) = rfull(r"(?P<a>you|your opponent) (?:have|has) (?P<d>less|fewer|more) (?P<w>.+?) than (?P<b>you|your opponent)", c) {
        let what = m["w"].replace("their", "your");
        let (a, b) = (side_of(&m["a"])?, side_of(&m["b"])?);
        let cmp = if &m["d"] == "more" { Cmp::Gt } else { Cmp::Lt };
        let w2 = what.replace(" on their field", " on your field");
        return Ok(Q::Cmp(counted(a, &w2)?, cmp, counted(b, &w2)?));
    }
    if let Some(m) = rfull(
        r"the number of DON!! cards on your field is (?P<d>equal to or less than|equal to or more than|less than|more than|equal to) the number on your opponent's field",
        c,
    ) {
        let cmp = match &m["d"] {
            "equal to or less than" => Cmp::Le,
            "equal to or more than" => Cmp::Ge,
            "less than" => Cmp::Lt,
            "more than" => Cmp::Gt,
            _ => Cmp::Eq,
        };
        return Ok(Q::Cmp(Num::DonField(Side::Me), cmp, Num::DonField(Side::Opp)));
    }
    if let Some(m) = rfull(r"you and your opponent have a total of (?P<n>\d+) or (?P<d>less|more) (?P<w>.+)", c) {
        let w = counted(Side::Both, &m["w"])?;
        return Ok(Q::Cmp(w, cmp_of(g(&m, "d")), Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"the only Characters on your field are (?P<ty>\{[^}]+\}(?: or \{[^}]+\})*) type Characters", c) {
        let pred = Pred { cats: vec![Category::Character], not_types: braces(&m["ty"]), ..Pred::default() };
        return Ok(Q::Not(Box::new(Q::exists(Sel::Cards { side: Side::Me, place: Place::Field, many: Many::All, pred }))));
    }
    if let Some(m) = rfull(r"there is (?:a|an) (?P<np>.+)", c) {
        let mut pred = np(&m["np"])?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        return Ok(Q::exists(Sel::Cards { side: Side::Both, place: Place::Field, many: Many::All, pred }));
    }
    // counts
    if let Some(m) = rfull(
        r"(?P<who>you|your opponent) (?:have|has) (?:a total of )?(?P<n>\d+)(?: or (?P<d>less|more))? (?P<w>.+)",
        c,
    ) {
        let side = side_of(&m["who"])?;
        let w = m["w"].replace("their ", "your ");
        return Ok(Q::Cmp(counted(side, &w)?, cmp_of(g(&m, "d")), Num::K(gi(&m, "n"))));
    }
    if let Some(m) = rfull(r"(?P<who>you|your opponent) (?:have|has) (?:any|a|an) (?P<w>.+)", c) {
        let side = side_of(&m["who"])?;
        let w = m["w"].replace("their ", "your ");
        return Ok(Q::Cmp(counted(side, &w)?, Cmp::Ge, Num::K(1)));
    }
    if let Some(m) = rfull(r"(?P<who>you|your opponent) (?:have|has) (?P<n>\[[^\]]+\](?: or \[[^\]]+\])*)(?: on your field)?", c) {
        let side = side_of(&m["who"])?;
        let names = crate::compile::find_all(r"\[([^\]]+)\]", &m["n"]);
        let pred = Pred { cats: vec![Category::Character], names, ..Pred::default() };
        return Ok(Q::exists(Sel::Cards { side, place: Place::Field, many: Many::All, pred }));
    }
    if let Some(m) = rfull(r"(?P<who>you|your opponent) (?:don't|do not|does not|doesn't) have (?:any |a |an )?(?P<w>.+)", c) {
        return Ok(Q::Not(Box::new(cond2(&format!("{} has a {}", &m["who"], &m["w"]))?)));
    }
    if let Some(m) = rfull(r"(?P<who>you|your opponent) (?:have|has) no (?P<w>.+)", c) {
        return Ok(Q::Not(Box::new(cond2(&format!("{} has a {}", &m["who"], &m["w"]))?)));
    }
    Err(Unsupported)
}

/// The first part of a line neither compiler reads (for the coverage report).
pub fn explain(raw: &str) -> String {
    let line = crate::compile::normalize(raw.trim_start_matches('•'));
    let mut body = line.clone();
    while let Some(m) = rmatch(r"(/)?\[([^\]]+)\]\s*", &body) {
        body = body[end(&m)..].to_string();
    }
    if let Some(m) = rmatch(r"^(.*?)\s*:\s*(.*)$", &body) {
        if costs2(&m[1]).is_ok() {
            body = m[2].to_string();
        } else if !m[1].contains(' ') || m[1].starts_with("You may") || m[1].starts_with("DON") {
            return format!("COST: {}", &m[1]);
        }
    }
    for s in sentences(&body) {
        let s = strip_then(&s);
        if s.starts_with("If you do") {
            continue;
        }
        if sentence(&s).is_err() {
            return explain_sentence(&s);
        }
    }
    body
}

fn explain_sentence(s: &str) -> String {
    let core = s.trim().trim_end_matches('.');
    if let Some(m) = rmatch(r"^(?:When (?P<a>.+?)|This effect can be activated (?:when|at) (?P<b>.+?)), (?P<rest>.+)$", core) {
        if events(g(&m, "a").or_else(|| g(&m, "b")).unwrap_or("")).is_some() {
            return explain_sentence(&cap(&m["rest"]));
        }
        return format!("EVENT: {}", g(&m, "a").or_else(|| g(&m, "b")).unwrap_or(""));
    }
    if let Some((_, rest)) = lead_condition(core) {
        return explain_sentence(&cap(&rest));
    }
    if let Some(c) = core.strip_prefix("If ") {
        return format!("COND: {}", c.split(", ").next().unwrap_or(c));
    }
    if let Some(x) = core.strip_prefix("You may ") {
        return explain_sentence(&cap(x));
    }
    core.to_string()
}

/// "Look at 5 cards from the top of your deck; reveal up to 1 X and add it to your hand. Then,
/// place the rest at the bottom of your deck in any order." Returns the body and how many
/// sentences it used; anything after "the rest" in the second sentence follows.
fn look(s: &str, next: Option<&str>) -> Option<(Body, usize)> {
    let m = rfull(
        concat!(
            r"(?:Look at|Reveal) (?:up to )?(?P<n>\d+) cards? from the top of your deck(?:; reveal (?:a total of )?up to (?:a total of )?(?P<k>\d+) (?P<f>.+?),? (?:and )?add (?:it|them) to your hand",
            r"| and add up to (?P<k2>\d+) (?P<f2>cards?) to your hand|(?: and|;|,) (?P<play>play) up to (?P<k3>\d+) (?P<f3>.+?)(?P<rested> rested)?",
            r"| and add up to (?P<k4>\d+) (?P<f4>.+?) to your hand",
            r"|(?:; | and )add up to (?P<k5>\d+) (?P<f5>.+?) to the top of your Life cards(?P<fu> face-up)?",
            r"| and trash up to (?P<k6>\d+) (?P<f6>cards?))",
            r"(?:(?:\.|,|, and| and) (?:[Tt]hen, )?(?P<r>place the rest .+|trash the rest.*))?\.?"
        ),
        s.trim(),
    )?;
    let k = ["k", "k2", "k3", "k4", "k5", "k6"].iter().find_map(|x| g(&m, x))?.parse().ok()?;
    let f = ["f", "f2", "f3", "f4", "f5", "f6"].iter().find_map(|x| g(&m, x))?;
    let dest = if g(&m, "k5").is_some() {
        LookTo::Life(g(&m, "fu").is_some())
    } else if g(&m, "k6").is_some() {
        LookTo::Trash
    } else if g(&m, "play").is_some() {
        LookTo::Play(g(&m, "rested").is_some())
    } else {
        LookTo::Hand
    };
    let pred = np(f).ok().or_else(|| either(f))?;
    let play = g(&m, "play").is_some();
    let (rest_text, used) = match g(&m, "r") {
        Some(r) => (r.trim_end_matches('.').to_string(), 1),
        None => match next {
            Some(n) => (strip_then(n).trim_end_matches('.').to_string(), 2),
            None if play => (String::new(), 1),
            None => return None,
        },
    };
    if play && !rest_text.contains("the rest") {
        // nothing said about a card not played: it stays on top of the deck
        let b = Body::Do(Do::Look { n: gu(&m, "n"), k, pred, rest: LookRest::Stay, to: dest });
        return Some((b, 1));
    }
    let r = rmatch(
        r"^(?:[Pp]lace the rest at the (?P<w>bottom|top or bottom) of (?:your|the) deck(?: in any order)?|[Tt]rash the rest)(?P<more>.*)$",
        &rest_text,
    )?;
    let rest = match g(&r, "w") {
        Some("bottom") => LookRest::Bottom,
        Some(_) => LookRest::TopOrBottom,
        None => LookRest::Trash,
    };
    let mut body = Body::Do(Do::Look { n: gu(&m, "n"), k, pred, rest, to: dest });
    let more = g(&r, "more").unwrap_or("").trim();
    if !more.is_empty() {
        let more = more.strip_prefix("and ").or_else(|| more.strip_prefix(", and ")).or_else(|| more.strip_prefix(", then ")).unwrap_or(more);
        body = Body::Seq(Box::new(body), Box::new(sentence(&cap(more)).ok()?));
    }
    Some((body, used))
}

/// "[Monkey.D.Luffy] or red Event": either noun phrase.
fn either(f: &str) -> Option<Pred> {
    for (i, _) in f.match_indices(" or ") {
        let right = f[i + 4..].trim_start_matches("up to ");
        let right = rmatch(r"^\d+ ", right).map_or(right, |m| &right[end(&m)..]);
        if let (Ok(mut a), Ok(b)) = (np_only(&f[..i]), np(right)) {
            // "{X} type Character cards or [Sanji] with a cost of 5 or less": the cost is for both
            if a.cost.is_empty() && a.power.is_empty() && b.names.len() > 0 {
                a.cost = b.cost.clone();
                a.power = b.power.clone();
            }
            let mut p = Pred::default();
            p.any_of = vec![a, b];
            return Some(p);
        }
    }
    None
}

/// Like compile::normalize, but parentheses inside a card name ("[Mr.3(Galdino)]") stay.
pub fn normalize2(raw: &str) -> String {
    let mut protected = String::new();
    let mut depth = 0;
    for ch in raw.chars() {
        match ch {
            '[' => depth += 1,
            ']' => depth -= 1,
            _ => {}
        }
        protected.push(match (depth > 0, ch) {
            (true, '(') => '\u{2985}',
            (true, ')') => '\u{2986}',
            _ => ch,
        });
    }
    crate::compile::normalize(&protected).replace('\u{2985}', "(").replace('\u{2986}', ")")
}

fn push_events(out: &mut Compiled, list: Vec<(Timing, Option<Q>)>, action: Body, raw: &str, spec: &Spec, optional: bool) {
    let group = if list.len() > 1 { Some(out.effects.len()) } else { None };
    for (t, q) in list {
        let mut e = Effect::new(t, action.clone(), raw);
        e.once_per_turn = spec.once_per_turn;
        e.don_req = spec.don_req;
        e.your_turn = spec.your_turn;
        e.opp_turn = spec.opp_turn;
        e.condition = q.map(|q| Cond::Q(Box::new(q)));
        e.optional = optional;
        e.opt_group = group; // one [Once Per Turn] for all of them
        out.effects.push(e);
    }
}

/// "When A or B": one effect per event.
fn events(ev: &str) -> Option<Vec<(Timing, Option<Q>)>> {
    let ev = ev.trim();
    if let Some(e) = event(ev) {
        return Some(vec![e]);
    }
    if rfull(r"a card is removed from your or your opponent's Life cards", ev).is_some() {
        return Some(vec![(Timing::LifeLeft, None), (Timing::OppLifeLeft, None)]);
    }
    if let Some(m) = rfull(r"your (?P<np>.+?) is removed from the field by your opponent's effect or K\.O\.'d", ev) {
        let mut pred = np(&m["np"]).ok()?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        return Some(vec![
            (Timing::Removed, Some(Q::Removed { owner: Side::Me, by_opp: Some(true), pred: pred.clone() })),
            (Timing::CharKo, Some(Q::Ko { owner: Some(Side::Me), by_opp: None, battle: Some(true), pred: Some(pred) })),
        ]);
    }
    if let Some(m) = rfull(r"your (?P<np>.+?) card is removed from the field", ev) {
        let mut pred = np(&m["np"]).ok()?;
        if pred.cats.is_empty() {
            pred.cats = vec![Category::Character];
        }
        return Some(vec![
            (Timing::Removed, Some(Q::Removed { owner: Side::Me, by_opp: None, pred: pred.clone() })),
            (Timing::CharKo, Some(Q::Ko { owner: Some(Side::Me), by_opp: None, battle: Some(true), pred: Some(pred) })),
        ]);
    }
    if let Some(m) = rfull(r"your opponent plays a Character (?P<q>with a base cost of \d+ or more), or when your opponent plays a Character using a Character's effect", ev) {
        let mut p = Pred { cats: vec![Category::Character], ..Pred::default() };
        let mut r = format!(" {}", &m["q"]);
        while !r.is_empty() {
            r = qualifier(&r, &mut p).ok()?;
        }
        return Some(vec![
            (Timing::OppPlay, Some(Q::Played { pred: p, from: None })),
            (Timing::OppPlay, Some(Q::PlayedByEffectOf(Pred { cats: vec![Category::Character], ..Pred::default() }))),
        ]);
    }
    if rfull(r"this Leader attacks or is attacked", ev).is_some() {
        return Some(vec![(Timing::WhenAttacking, None), (Timing::OnOppAttack, Some(Q::TargetIsThis))]);
    }
    let m = rfull(r"your opponent activates (?P<a>an Event|\[Blocker\]|\[Trigger\]) or (?P<b>an Event|\[Blocker\]|\[Trigger\])", ev)
        .or_else(|| rfull(r"(?P<a>you take damage) or (?P<b>.+)", ev))?;
    let one = |x: &str| match x {
        "you take damage" => Some((Timing::TookDamage, None)),
        x if x.starts_with("your ") || x.starts_with("one of") => event(x),
        x => event(&format!("your opponent activates {x}")),
    };
    Some(vec![one(&m["a"])?, one(&m["b"])?])
}

/// "When <event>, ...": the timing it fires at, and any extra condition.
fn event(ev: &str) -> Option<(Timing, Option<Q>)> {
    let ev = ev.trim();
    if let Some(m) = rfull(r"(?P<who>you play|your opponent plays) a Character (?P<q>with .+?)(?P<z> from your (?:hand|trash))?", ev) {
        let mut p = Pred { cats: vec![Category::Character], ..Pred::default() };
        let mut rest = format!(" {}", &m["q"]);
        while !rest.is_empty() {
            rest = qualifier(&rest, &mut p).ok()?;
        }
        let from = g(&m, "z").map(|z| if z.contains("hand") { Zone::Hand } else { Zone::Trash });
        let t = if m["who"].starts_with("you ") { Timing::MyPlay } else { Timing::OppPlay };
        return Some((t, Some(Q::Played { pred: p, from })));
    }
    if let Some(m) = rfull(r"(?:an? )?(?P<np>.+?) is played from your (?P<z>hand|trash)", ev) {
        if let Ok(pred) = np(&m["np"]) {
            let from = Some(if &m["z"] == "hand" { Zone::Hand } else { Zone::Trash });
            return Some((Timing::MyPlay, Some(Q::Played { pred, from })));
        }
    }
    if rfull(r"a DON!! card on your field is returned to your DON!! deck by your effect", ev).is_some() {
        return Some((Timing::DonReturned, Some(Q::DonReturnedByMe)));
    }
    if let Some(m) = rfull(r"a card is trashed from your hand by your (?P<np>.+?) card's effect", ev) {
        return Some((Timing::HandTrashed, Some(Q::HandTrashedBy(np(&format!("{} card", &m["np"])).ok()?))));
    }
    if rfull(r"you draw a card outside of your Draw Phase", ev).is_some() {
        return Some((Timing::Drew, None));
    }
    if rfull(r"your number of Life cards becomes 0", ev).is_some() {
        return Some((Timing::LifeZero, None));
    }
    if rfull(r"a card is added to your hand from your Life", ev).is_some() {
        return Some((Timing::LifeLeft, Some(Q::LifeWentToHand)));
    }
    if rfull(r"a Character is removed from the field by your effect", ev).is_some() {
        return Some((Timing::Removed, Some(Q::RemovedByMe)));
    }
    if rfull(r"your opponent's Character is returned to the owner's hand by your effect", ev).is_some() {
        let pred = Pred { cats: vec![Category::Character], ..Pred::default() };
        return Some((Timing::Removed, Some(Q::And(vec![Q::Removed { owner: Side::Opp, by_opp: None, pred }, Q::RemovedByMe, Q::RemovedTo(Zone::Hand)]))));
    }
    if rfull(r"this Character (?:is|becomes) rested by your opponent's effect", ev).is_some() {
        return Some((Timing::RestedByOpp, None));
    }
    if rfull(r"this Character becomes rested by your opponent's Character's effect", ev).is_some() {
        return Some((Timing::RestedByOpp, Some(Q::RestedBySrc(Pred { cats: vec![Category::Character], ..Pred::default() }))));
    }
    if rfull(r"a card is trashed from your hand by an effect", ev).is_some() {
        return Some((Timing::HandTrashed, None));
    }
    if rfull(r"your opponent activates (?:a )?\[Blocker\]", ev).is_some() {
        return Some((Timing::OppBlock, None));
    }
    if rfull(r"your opponent activates an Event", ev).is_some() {
        return Some((Timing::OppEvent, None));
    }
    if rfull(r"your opponent activates (?:a )?\[Trigger\]", ev).is_some() {
        return Some((Timing::OppTrigger, None));
    }
    if rfull(r"a \[Trigger\] activates", ev).is_some() {
        return Some((Timing::TriggerUsed, None));
    }
    if rfull(r"a card is removed from your opponent's Life cards", ev).is_some() {
        return Some((Timing::OppLifeLeft, None));
    }
    if rfull(r"a card is removed from your Life cards", ev).is_some() {
        return Some((Timing::LifeLeft, None));
    }
    if rfull(r"this Leader or any of your Characters is given a DON!! card", ev).is_some() {
        return Some((Timing::DonGiven, None));
    }
    if let Some(m) = rfull(r"this Character battles (?P<np>.+)", ev) {
        if let Ok(pred) = np(&m["np"]) {
            return Some((Timing::AttackDeclared, Some(Q::BattlesWith(pred))));
        }
    }
    if let Some(m) = rfull(r"your (?P<np>.+?) is removed from the field by (?P<how>your opponent's effect|an effect)", ev) {
        if let Ok(mut pred) = np(m["np"].trim_end_matches(" card")) {
            if pred.cats.is_empty() {
                pred.cats = vec![Category::Character];
            }
            let by_opp = if m["how"].contains("opponent") { Some(true) } else { None };
            return Some((Timing::Removed, Some(Q::Removed { owner: Side::Me, by_opp, pred })));
        }
    }
    if let Some(m) = rfull(r"(?:one of )?your (?P<np>.+?) is K\.O\.'d", ev) {
        if let Ok(mut pred) = np(&m["np"]) {
            if pred.cats.is_empty() {
                pred.cats = vec![Category::Character];
            }
            return Some((Timing::CharKo, Some(Q::Ko { owner: Some(Side::Me), by_opp: None, battle: None, pred: Some(pred) })));
        }
    }
    if rfull(r"this Character becomes rested", ev).is_some() {
        return Some((Timing::BecameRested, None));
    }
    if let Some(m) = rfull(r"this Character is K\.O\.'d(?P<how> by your opponent's effect| by an effect)?", ev) {
        let q = match g(&m, "how") {
            Some(h) if h.contains("opponent") => Some(Q::Ko { owner: None, by_opp: Some(true), battle: Some(false), pred: None }),
            Some(_) => Some(Q::Ko { owner: None, by_opp: None, battle: Some(false), pred: None }),
            None => None,
        };
        return Some((Timing::OnKo, q));
    }
    if let Some(m) = rfull(r"(?P<w>a|your opponent's|one of your) Character is K\.O\.'d", ev) {
        let owner = match &m["w"] {
            "a" => None,
            "your opponent's" => Some(Side::Opp),
            _ => Some(Side::Me),
        };
        return Some((Timing::CharKo, Some(Q::Ko { owner, by_opp: None, battle: None, pred: None })));
    }
    if rfull(r"this Character battles and K\.O\.'s your opponent's Character", ev).is_some() {
        return Some((Timing::BattleKo, None));
    }
    if rfull(r"you deal damage to your opponent's Life", ev).is_some() {
        return Some((Timing::MyDamage, None));
    }
    if rfull(r"this Leader attacks your opponent's Leader", ev).is_some() {
        return Some((Timing::WhenAttacking, Some(Q::AttackingLeader)));
    }
    if rfull(r"a DON!! card on (?:your|the) field is returned to your DON!! deck", ev).is_some() {
        return Some((Timing::DonReturned, None));
    }
    if let Some(m) = rfull(r"(?P<n>\d+) or more DON!! cards on your field are returned to your DON!! deck", ev) {
        return Some((Timing::DonReturned, Some(Q::Cmp(Num::DonJustReturned, Cmp::Ge, Num::K(gi(&m, "n"))))));
    }
    if rfull(r"this (?:Character|Leader)'s attack deals damage to your opponent's Life", ev).is_some() {
        return Some((Timing::DealtDamage, None));
    }
    if rfull(r"you activate an Event", ev).is_some() {
        return Some((Timing::MyEvent, None));
    }
    if rfull(r"your opponent activates an Event", ev).is_some() {
        return Some((Timing::OppEvent, None));
    }
    if rfull(r"you play a Character", ev).is_some() {
        return Some((Timing::MyPlay, None));
    }
    if rfull(r"your opponent plays a Character", ev).is_some() {
        return Some((Timing::OppPlay, None));
    }
    None
}

/// "Play up to 1 each of [A], [B], and [C] with a cost of 2 from your hand": one play per name.
fn play_each(names: &[String], q: &str, zone: &str) -> Res<Body> {
    let mut parts = vec![];
    for n in names {
        parts.push(clause_gen(&format!("Play up to 1 [{n}] {q} from your {zone}"))?);
    }
    let mut it = parts.into_iter().rev();
    let mut acc = it.next().ok_or(Unsupported)?;
    for p in it {
        acc = Body::Seq(Box::new(p), Box::new(acc));
    }
    Ok(acc)
}

/// For debugging: which pieces of a sentence read (`cargo run --example try -- "text"`).
pub fn debug(text: &str) -> String {
    let mut out = vec![format!("sentence: {}", sentence(text).is_ok())];
    let core = text.trim().trim_end_matches('.');
    out.push(format!("clauses: {}", clauses(core).is_ok()));
    out.push(format!("clause_gen: {}", clause_gen(core).is_ok()));
    if let Some((_, rest)) = lead_condition(core) {
        out.push(format!("after condition: {rest:?} -> {}", clauses(&cap(&rest)).is_ok()));
    }
    out.join("\n")
}

pub fn debug_sel(text: &str) -> String {
    match sel(text) {
        Ok(s) => crate::gen::show_sel(&s),
        Err(_) => "no".into(),
    }
}

pub fn debug_line(raw: &str) -> String {
    let mut out = Compiled::default();
    let r = compile_line2(&normalize2(raw), raw, &mut out, &[]);
    format!("{} statics={} no_ko_battle_by={}", r.is_ok(), out.static_keywords.len(), out.no_ko_battle_by.len())
}
