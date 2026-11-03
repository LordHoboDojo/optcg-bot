//! Compile printed effect text into effects, ported from optcg/effect_text.py.
//!
//! The patterns, their order and the phrases that make a line unsupported are the same as in
//! Python, so both engines implement exactly the same lines. Python's `re.match` is a regex
//! anchored at the start and `re.fullmatch` one anchored at both ends; a group that matched
//! the empty string counts as absent, as it does in Python's truthiness checks.

use std::cell::RefCell;
use std::collections::HashMap;

use regex::{Captures, Regex};

use crate::cards::*;
use crate::effects::*;

pub struct Unsupported;
pub(crate) type Res<T> = Result<T, Unsupported>;

thread_local! {
    static CACHE: RefCell<HashMap<String, Regex>> = RefCell::new(HashMap::new());
}

pub(crate) fn re(pat: String) -> Regex {
    CACHE.with(|c| {
        c.borrow_mut().entry(pat.clone()).or_insert_with(|| Regex::new(&pat).expect("bad pattern")).clone()
    })
}

/// `re.match`
pub(crate) fn rmatch<'t>(pat: &str, text: &'t str) -> Option<Captures<'t>> {
    re(format!("^(?:{pat})")).captures(text)
}

/// `re.fullmatch`
pub(crate) fn rfull<'t>(pat: &str, text: &'t str) -> Option<Captures<'t>> {
    re(format!("^(?:{pat})$")).captures(text)
}

pub(crate) fn find_all(pat: &str, text: &str) -> Vec<String> {
    re(pat.to_string()).captures_iter(text).map(|c| c[1].to_string()).collect()
}

/// A named group, or None when it didn't take part or matched nothing (Python truthiness).
pub(crate) fn g<'t>(c: &Captures<'t>, name: &str) -> Option<&'t str> {
    c.name(name).map(|m| m.as_str()).filter(|s| !s.is_empty())
}

pub(crate) fn gi(c: &Captures, name: &str) -> i32 {
    g(c, name).unwrap().parse().unwrap()
}

pub(crate) fn gu(c: &Captures, name: &str) -> usize {
    gi(c, name) as usize
}

pub(crate) fn end(c: &Captures) -> usize {
    c.get(0).unwrap().end()
}

pub(crate) fn cap(s: &str) -> String {
    let mut ch = s.chars();
    match ch.next() {
        Some(f) => f.to_uppercase().collect::<String>() + ch.as_str(),
        None => String::new(),
    }
}

pub(crate) fn circled(c: char) -> Option<u32> {
    "➀➁➂➃➄➅➆➇➈➉".chars().position(|x| x == c).or_else(|| "①②③④⑤⑥⑦⑧⑨⑩".chars().position(|x| x == c)).map(|i| i as u32 + 1)
}

pub fn normalize(line: &str) -> String {
    let notes = re(r"\([^()]*\)".to_string());
    let mut line = line.to_string();
    loop {
        // drop explanatory notes (2-8-4), including nested ones
        let next = notes.replace_all(&line, "").into_owned();
        if next == line {
            break;
        }
        line = next;
    }
    for (a, b) in [
        ("−", "-"),
        ("–", "-"),
        ("－", "-"),
        ("’", "'"),
        ("＜", "<"),
        ("＞", ">"),
        ("“", "\""),
        ("”", "\""),
        ("Charactes", "Characters"), // a misprint on OP17-076
    ] {
        line = line.replace(a, b);
    }
    re(r"\s+".to_string()).replace_all(&line, " ").trim().to_string()
}

/// The lines of card text that are effects (not just explanatory notes).
pub fn effect_lines(text: &str) -> Vec<String> {
    text.split('\n').filter(|l| !normalize(l).is_empty()).map(|l| l.trim().to_string()).collect()
}

#[derive(Default)]
pub struct Compiled {
    pub effects: Vec<Effect>,
    pub keywords: Vec<Kw>,
    pub static_power: Vec<(Cond, i32, Target)>,
    pub static_keywords: Vec<(Cond, Kw, Target)>,
    pub static_cost: Vec<(Cond, i32, Target)>,
    pub hand_cost: Vec<(Cond, i32)>,
    pub gen_replacements: Vec<(crate::gen::RepData, String, bool)>,
    pub aliases: Vec<String>,
    pub gen_power: Vec<(Cond, crate::gen::Num, Target)>,
    pub gen_base_power: Vec<(Cond, crate::gen::Num, Target)>,
    pub no_ko_battle_by: Vec<(Cond, Target, crate::gen::Pred)>,
    pub hand_discount: Vec<(Cond, crate::gen::Pred, i32)>,
    pub deck_out_at_end: bool,
    pub deck_out_wins: bool,
    pub don_deck_size: Option<u32>,
    pub gen_cost: Vec<(Cond, crate::gen::Num, Target)>,
    pub gen_counter: Vec<(Cond, crate::gen::Pred, i32)>,
    pub no_ko_effect_by: Vec<(Cond, Target, crate::gen::Pred)>,
    pub play_rested: bool,
    pub don_phase_to_leader: bool,
    pub faceup_life_to_deck: bool,
    pub no_own_on_play: bool,
    pub no_effect_play: bool,
    pub unimplemented: Vec<String>,
    /// lines the second compiler read
    pub second: Vec<String>,
}

pub fn compile_card(effect_text: &str, trigger_text: &str) -> Compiled {
    compile_card_with(effect_text, trigger_text, &|_| false)
}

/// `scripted(line)`: a hand-written script implements this line, so the second compiler leaves it.
pub fn compile_card_with(effect_text: &str, trigger_text: &str, scripted: &dyn Fn(&str) -> bool) -> Compiled {
    let mut out = Compiled::default();
    let mut groups: Vec<(String, Vec<String>)> = vec![];
    for raw in effect_lines(&format!("{effect_text}\n{trigger_text}")) {
        // options of "choose one:" are bulleted with • (some cards use -)
        let dash = raw.starts_with("- ") && groups.last().map_or(false, |(g, b)| g.ends_with(" one:") || !b.is_empty() && b[0].starts_with("- "));
        if (raw.starts_with('•') || dash) && !groups.is_empty() {
            groups.last_mut().unwrap().1.push(raw);
        } else {
            groups.push((raw, vec![]));
        }
    }
    for (raw, bullets) in groups {
        let norm: Vec<String> = bullets.iter().map(|b| normalize(b.trim_start_matches('•').trim_start_matches("- "))).collect();
        // lines the first compiler (kept in step with Python) can't read go to the second
        if compile_line(&normalize(&raw), &raw, &mut out, &norm).is_err()
            && !scripted(&raw)
            && {
                let ok = crate::compile2::compile_line2(&crate::compile2::normalize2(&raw), &raw, &mut out, &norm).is_ok();
                if ok {
                    out.second.push(raw.clone());
                }
                !ok
            }
        {
            out.unimplemented.push(raw);
            out.unimplemented.extend(bullets);
        }
    }
    out
}

// ---------------------------------------------------------------- line structure

#[derive(Default)]
pub(crate) struct Spec {
    pub once_per_turn: bool,
    pub don_req: u32,
    pub your_turn: bool,
    pub opp_turn: bool,
}

#[derive(Default)]
pub(crate) struct ParsedCosts {
    pub rest_don: Option<u32>,
    pub don_minus: Option<u32>,
    pub rest_self: bool,
    pub k: Costs,
    pub any_resolving: bool,
}

const COST_PARTS: [(&str, &str); 18] = [
    (r"([➀-➉①-⑩])\s*", "rest_don"),
    (r"DON!! -(\d+)\s*", "don_minus"),
    (r"You may rest (\d+) of your DON!! cards?\s*", "rest_don"),
    (r"(?:You may )?rest this (?:Character|Stage|Leader)\s*", "rest_self"),
    (r"(?:and )?this (?:Character|Stage)\s*", "rest_self"),
    (r"(?:You may )?trash this (?:Character|Stage)\s*", "trash_self"),
    (r"(?:You may )?trash (\d+) (.+?) from your hand\s*", "trash"),
    (r"(?:You may )?add (\d+) cards? from the top of your Life cards to your hand\s*", "life"),
    (r"(?:You may )?rest (\d+) of your cards\s*", "rest_cards"),
    (r"(?:You may )?turn (\d+) cards? from the top of your Life cards face-up\s*", "life_face_up"),
    (r"(?:You may )?turn (\d+) cards? from the top of your Life cards face-down\s*", "life_face_down"),
    (r"(?:You may )?reveal (?:a total of )?(\d+) (.+?) from your hand\s*", "reveal"),
    (r"(?:You may )?give (\d+) active DON!! cards? to 1 of your Leader\s*", "give_leader_don"),
    (r"(?:You may )?return (\d+) of your active DON!! cards? to your DON!! deck\s*", "return_active_don"),
    (r"(?:You may )?rest your Leader \[([^\]]+)\]\s*", "rest_leader"),
    (r"(?:You may )?K\.O\. (\d+) of your (.+?)\s*$", "ko_own"),
    (r"(?:You may )?trash (\d+) of your (.+?)\s*$", "trash_own"),
    (r"(?:and|,)\s*", ""),
];

pub(crate) fn parse_costs(prefix: &str) -> Res<ParsedCosts> {
    let mut c = ParsedCosts::default();
    let mut rest = prefix.to_string();
    while !rest.is_empty() {
        let mut hit = false;
        for (pat, kind) in COST_PARTS {
            let Some(m) = rmatch(pat, &rest) else { continue };
            let n = || m.get(1).unwrap().as_str();
            let num = || n().parse::<usize>().unwrap();
            match kind {
                "rest_don" => {
                    let s = n();
                    let ch = s.chars().next().unwrap();
                    c.rest_don = Some(circled(ch).unwrap_or_else(|| s.parse().unwrap()));
                }
                "don_minus" => c.don_minus = Some(num() as u32),
                "rest_self" => c.rest_self = true,
                "trash_self" => {
                    c.k.trash_self = true;
                    c.any_resolving = true;
                }
                "trash" => {
                    c.k.trash = Some((num(), parse_filter(&m[2])?));
                    c.any_resolving = true;
                }
                "reveal" => {
                    c.k.reveal = Some((num(), parse_filter(&m[2])?));
                    c.any_resolving = true;
                }
                "life" => {
                    c.k.life = num();
                    c.any_resolving = true;
                }
                "rest_cards" => {
                    c.k.rest_cards = num();
                    c.any_resolving = true;
                }
                "life_face_up" => {
                    c.k.life_face_up = num();
                    c.any_resolving = true;
                }
                "life_face_down" => {
                    c.k.life_face_down = num();
                    c.any_resolving = true;
                }
                "give_leader_don" => {
                    c.k.give_leader_don = num() as u32;
                    c.any_resolving = true;
                }
                "return_active_don" => {
                    c.k.return_active_don = num() as u32;
                    c.any_resolving = true;
                }
                "rest_leader" => {
                    c.k.rest_leader = Some(n().to_string());
                    c.any_resolving = true;
                }
                "ko_own" => {
                    c.k.ko_own = Some((num(), own_candidates(&m[2], true)?));
                    c.any_resolving = true;
                }
                "trash_own" => {
                    c.k.trash_own = Some((num(), own_candidates(&m[2], true)?));
                    c.any_resolving = true;
                }
                _ => {}
            }
            rest = rest[end(&m)..].to_string();
            hit = true;
            break;
        }
        if !hit {
            return Err(Unsupported);
        }
    }
    Ok(c)
}

pub(crate) const CHOOSE_ONE: &str =
    r"(?:(?P<pre>.+?),? then |(?P<pre2>.+?\.) Then, )?(?P<who>Your opponent chooses|[Cc]hoose) one:";

fn compile_line(line: &str, raw: &str, out: &mut Compiled, bullets: &[String]) -> Res<()> {
    let mut tags: Vec<String> = vec![];
    let mut alternatives = false;
    let mut line = line.to_string();
    while let Some(m) = rmatch(r"(/)?\[([^\]]+)\]\s*", &line) {
        alternatives |= m.get(1).is_some();
        tags.push(m[2].to_string());
        line = line[end(&m)..].to_string();
    }
    let body = line;
    for t in &tags {
        if let Some(k) = Kw::printed(t) {
            if !out.keywords.contains(&k) {
                out.keywords.push(k);
            }
        }
    }
    let tags: Vec<String> = tags.into_iter().filter(|t| Kw::printed(t).is_none()).collect();
    if tags.is_empty() && body.is_empty() {
        return Ok(());
    }

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
        if !bullets.is_empty() {
            return Err(Unsupported);
        }
        return compile_static(&body, &spec, out);
    }

    let mut costs = ParsedCosts::default();
    let mut body = body;
    if let Some(m) = rmatch(r"^(.*?)\s*:\s*(.*)$", &body) {
        // text before the first colon is a cost only if it reads as one ("[Rush: Character]" isn't)
        if let Ok(c) = parse_costs(&m[1]) {
            costs = c;
            body = m[2].to_string();
        }
    }

    let action = if let Some(cm) = rfull(CHOOSE_ONE, &body) {
        if bullets.is_empty() {
            return Err(Unsupported);
        }
        let mut action = choose_one(bullets, cm["who"].starts_with("Your opponent"))?;
        if let Some(pre) = g(&cm, "pre").or_else(|| g(&cm, "pre2")) {
            let first = parse_body(&(cap(pre.trim_end_matches('.')) + "."))?;
            action = Body::Seq(Box::new(first), Box::new(action));
        }
        action
    } else if !bullets.is_empty() {
        return Err(Unsupported);
    } else {
        parse_body(&body)?
    };

    let engine_costs = costs.rest_don.is_some() || costs.don_minus.is_some() || costs.rest_self;
    let group = if timings.len() > 1 { Some(out.effects.len()) } else { None };
    for t in timings {
        let ask = !matches!(t, Timing::ActivateMain | Timing::Main | Timing::Counter); // PLAYER_CHOSEN
        let (body, condition) = if costs.any_resolving {
            let k = Box::new(costs.k.clone());
            (
                Body::WithCosts { costs: k.clone(), ask: ask && !engine_costs, body: Box::new(action.clone()) },
                Some(Cond::Payable(k)),
            )
        } else {
            (action.clone(), None)
        };
        let mut e = Effect::new(t, body, raw);
        e.once_per_turn = spec.once_per_turn;
        e.don_req = spec.don_req;
        e.your_turn = spec.your_turn;
        e.opp_turn = spec.opp_turn;
        e.rest_don = costs.rest_don.unwrap_or(0);
        e.don_minus = costs.don_minus.unwrap_or(0);
        e.rest_self = costs.rest_self;
        e.condition = condition;
        e.optional = engine_costs && ask;
        e.opt_group = group;
        out.effects.push(e);
    }
    Ok(())
}

fn compile_static(body: &str, spec: &Spec, out: &mut Compiled) -> Res<()> {
    let mut body = body.to_string();
    let mut extra = None;
    if let Some((c, rest)) = match_condition(&body) {
        extra = Some(Box::new(c));
        body = cap(&rest);
    }
    let cond = Cond::Spec { don_req: spec.don_req, your_turn: spec.your_turn, opp_turn: spec.opp_turn, extra };

    if rfull(r"This (?:Character|Leader) cannot attack\.", &body).is_some() {
        out.static_keywords.push((cond, Kw::CannotAttack, Target::SelfCard));
        return Ok(());
    }
    if let Some(m) = rfull(r"Give this card in your hand -(\d+) cost\.", &body) {
        out.hand_cost.push((cond, -m[1].parse::<i32>().unwrap()));
        return Ok(());
    }
    let (to, rest) = if let Some(m) = rmatch(r"This (?:Character|Leader|card) ", &body) {
        (Target::SelfCard, body[end(&m)..].to_string())
    } else if let Some(m) = rmatch(
        r"All of your (?:(?P<types>\{[^}]+\}) type )?Characters(?: with a cost of (?P<c>\d+) or more)? ",
        &body,
    ) {
        let types = g(&m, "types").map(|t| t[1..t.len() - 1].to_string());
        let min_cost = g(&m, "c").map(|c| c.parse().unwrap());
        (Target::AllYourCharacters { types, min_cost }, body[end(&m)..].to_string())
    } else {
        return Err(Unsupported);
    };
    if let Some(km) = rfull(
        r"cannot be K\.O\.'d (?P<how>in battle|by effects|by your opponent's effects)(?: and gains \[(?P<kw>[^\]]+)\])?\.",
        &rest,
    ) {
        let kw = match g(&km, "kw") {
            Some(k) => Some(Kw::printed(k).ok_or(Unsupported)?),
            None => None,
        };
        let marker = match &km["how"] {
            "in battle" => Kw::NoKoInBattle,
            "by effects" => Kw::NoKoByEffects,
            _ => Kw::NoKoByOppEffects,
        };
        out.static_keywords.push((cond.clone(), marker, to.clone()));
        if let Some(k) = kw {
            out.static_keywords.push((cond, k, to));
        }
        return Ok(());
    }
    // rest is e.g. "gains +3000 power.", "gains [Blocker] and +3000 power.", "gain +12 cost."
    let Some(parts) = rfull(r"gains? (?:\[(?P<kw>[^\]]+)\])?(?: and )?(?:\+(?P<v>\d+) (?P<what>power|cost))?\.", &rest)
    else {
        return Err(Unsupported);
    };
    if g(&parts, "kw").is_none() && g(&parts, "v").is_none() {
        return Err(Unsupported);
    }
    if let Some(k) = g(&parts, "kw") {
        let k = Kw::printed(k).ok_or(Unsupported)?;
        out.static_keywords.push((cond.clone(), k, to.clone()));
    }
    if g(&parts, "v").is_some() {
        let v = gi(&parts, "v");
        if &parts["what"] == "power" {
            out.static_power.push((cond, v, to));
        } else {
            out.static_cost.push((cond, v, to));
        }
    }
    Ok(())
}

fn choose_one(bullets: &[String], opponent: bool) -> Res<Body> {
    let mut options = vec![];
    for b in bullets {
        options.push((b.clone(), parse_body(&cap(b))?));
    }
    Ok(Body::ChooseOne { opponent, options })
}

// ---------------------------------------------------------------- conditions

pub(crate) fn cond_part(clause: &str) -> Option<Cond> {
    let dir = |s: &str| if s == "less" { Dir::Less } else { Dir::More };
    let num = |m: &Captures, i: usize| m[i].parse::<i32>().unwrap();
    if let Some(m) = rfull(r"your Leader is \[([^\]]+)\] or has the \{([^}]+)\} type", clause) {
        return Some(Cond::LeaderIsOrHasType(m[1].into(), m[2].into()));
    }
    if let Some(m) = rfull(r"your Leader has the \{([^}]+)\} type", clause) {
        return Some(Cond::LeaderHasType(m[1].into()));
    }
    if let Some(m) = rfull(r#"your Leader's type includes "([^"]+)""#, clause) {
        return Some(Cond::LeaderTypeIncludes(m[1].into()));
    }
    if let Some(m) = rfull(r"your Leader is \[([^\]]+)\]", clause) {
        return Some(Cond::LeaderIs(m[1].into()));
    }
    if let Some(m) = rfull(r"your opponent's Leader has (\d+) power or more", clause) {
        return Some(Cond::OppLeaderPowerAtLeast(num(&m, 1)));
    }
    if let Some(m) = rfull(r"your opponent has (\d+) or (less|more) Life cards?", clause) {
        return Some(Cond::OppLife(num(&m, 1), dir(&m[2])));
    }
    if let Some(m) = rfull(r"you have (\d+) or (less|more) Life cards?", clause) {
        return Some(Cond::MyLife(num(&m, 1), dir(&m[2])));
    }
    if let Some(m) = rfull(r"you have (\d+) or (less|more) cards in your hand", clause) {
        return Some(Cond::MyHand(num(&m, 1), dir(&m[2])));
    }
    if let Some(m) = rfull(r"your opponent has (\d+) or (less|more) cards in their hand", clause) {
        return Some(Cond::OppHand(num(&m, 1), dir(&m[2])));
    }
    if let Some(m) = rfull(r"you have (\d+) or more cards in your trash", clause) {
        return Some(Cond::MyTrashAtLeast(num(&m, 1) as usize));
    }
    if let Some(m) = rfull(r"you have no Characters with a cost of (\d+) or more", clause) {
        return Some(Cond::NoCharCostAtLeast(num(&m, 1)));
    }
    if let Some(m) = rfull(r"your Leader has the <([^>]+)> attribute", clause) {
        return Some(Cond::LeaderHasAttr(m[1].into()));
    }
    if let Some(m) = rfull(r"your opponent has a Character with (\d+) base power or more", clause) {
        return Some(Cond::OppCharBasePowerAtLeast(num(&m, 1)));
    }
    if let Some(m) = rfull(r"your opponent has a Character with a cost of (\d+)", clause) {
        return Some(Cond::OppCharCostIs(num(&m, 1)));
    }
    if let Some(m) = rfull(
        r"the number of DON!! cards on your field is at least (\d+) less than the number on your opponent's field",
        clause,
    ) {
        return Some(Cond::DonBehindBy(num(&m, 1)));
    }
    if let Some(m) = rfull(r"you have (\d+) or more DON!! cards on your field", clause) {
        return Some(Cond::DonOnFieldAtLeast(num(&m, 1) as u32));
    }
    if let Some(m) = rfull(r"there is a Character with a cost of (\d+) or more", clause) {
        return Some(Cond::AnyCharCostAtLeast(num(&m, 1)));
    }
    if let Some(m) = rfull(r"you have a Character with a cost of (\d+) or more", clause) {
        return Some(Cond::MyCharCostAtLeast(num(&m, 1)));
    }
    if let Some(m) = rfull(r"your opponent has a Character with (\d+) power or more", clause) {
        return Some(Cond::OppCharPowerAtLeast(num(&m, 1)));
    }
    if let Some(m) = rfull(r"you have no other \[([^\]]+)\]", clause) {
        return Some(Cond::NoOther(m[1].into()));
    }
    if let Some(m) = rfull(r"you have (\d+) or more Characters with (\d+) base power or more", clause) {
        return Some(Cond::CharsWithBasePower(num(&m, 1) as usize, num(&m, 2)));
    }
    if let Some(m) = rfull(r"you have (\d+) or more Characters with a \[Trigger\]", clause) {
        return Some(Cond::CharsWithTrigger(num(&m, 1) as usize));
    }
    if rfull(r"this Character was played on this turn", clause).is_some() {
        return Some(Cond::PlayedThisTurn);
    }
    if rfull(r"you have a face-up Life card", clause).is_some() {
        return Some(Cond::FaceUpLife);
    }
    None
}

/// 'If A and B, rest' -> (condition, rest), or None if the text doesn't start with a known condition.
pub(crate) fn match_condition(text: &str) -> Option<(Cond, String)> {
    let m = rmatch(r"If (.+?), ", text)?;
    let mut conds = vec![];
    for clause in m[1].split(" and ") {
        conds.push(cond_part(clause)?);
    }
    Some((Cond::All(conds), text[end(&m)..].to_string()))
}

// ---------------------------------------------------------------- bodies

pub fn parse_body(text: &str) -> Res<Body> {
    let text = text.trim();
    if text.is_empty() {
        return Err(Unsupported);
    }
    if let Some((cond, rest)) = match_condition(text) {
        let inner = parse_body(&cap(&rest))?;
        return Ok(Body::If(cond, Box::new(inner)));
    }
    if let Some(m) = rfull(r"Your opponent may trash (\d+) cards? from their hand\. If they do not, (.+)", text) {
        let otherwise = parse_body(&cap(&m[2]))?;
        return Ok(Body::UnlessOppTrashes(m[1].parse().unwrap(), Box::new(otherwise)));
    }
    if let Some(m) = rfull(r"(You may [^:]+?):\s*(.+)", text) {
        if let Ok(c) = parse_costs(&m[1]) {
            let engine = c.rest_don.is_some() || c.don_minus.is_some() || c.rest_self;
            if c.any_resolving && !engine {
                let body = parse_body(&cap(&m[2]))?;
                return Ok(Body::WithCosts { costs: Box::new(c.k), ask: true, body: Box::new(body) });
            }
        }
    }
    if let Some(rest) = text.strip_prefix("You may ") {
        if rmatch(r"You may (trash|rest|add) .*:", text).is_none() {
            return Ok(Body::Optional(Box::new(parse_body(&cap(rest))?)));
        }
    }
    for (pat, build) in templates() {
        let Some(m) = rmatch(&pat, text) else { continue };
        let first = build(&m)?;
        let mut rest = text[end(&m)..].trim();
        if let Some(r) = rest.strip_prefix("Then, ") {
            rest = r;
        }
        if rest.is_empty() {
            return Ok(first);
        }
        return Ok(Body::Seq(Box::new(first), Box::new(parse_body(&cap(rest))?)));
    }
    Err(Unsupported)
}

// ---------------------------------------------------------------- targets

const OPP: &str = r"(?P<n>\d+) of your opponent's (?P<rested>rested )?Characters(?: with (?:a (?P<basec>base )?cost of (?P<cost>\d+)(?P<orless> or less)?|(?P<pow>\d+) (?P<basep>base )?power or less))?";

fn opp_pred(m: &Captures) -> OppPred {
    OppPred {
        rested: g(m, "rested").is_some(),
        cost: g(m, "cost").map(|c| (c.parse().unwrap(), g(m, "basec").is_some(), g(m, "orless").is_some())),
        power: g(m, "pow").map(|p| (p.parse().unwrap(), g(m, "basep").is_some())),
    }
}

/// Card filter phrases: 'card', '{Straw Hat Crew} type card other than [Nami]', ...
pub fn parse_filter(f: &str) -> Res<Filter> {
    let f = f.trim();
    let cost_dir = |d: Option<&str>| match d {
        Some("more") => Dir::More,
        Some("less") => Dir::Less,
        _ => Dir::Eq,
    };
    if let Some(m) = rfull(r"((?:\[[^\]]+\](?:,? or |, )?)+)(?: with a cost of (\d+)(?: or (less|more))?)?", f) {
        let names = find_all(r"\[([^\]]+)\]", &m[1]);
        let cost = m.get(2).map(|n| (n.as_str().parse().unwrap(), cost_dir(m.get(3).map(|x| x.as_str()))));
        return Ok(Filter::Names { names, cost });
    }
    let Some(m) = rfull(
        concat!(
            r"(?:(?P<color>red|green|blue|purple|black|yellow) )?",
            r"(?:(?P<types>\{[^}]+\}(?: or \{[^}]+\})*) type )?",
            r"(?:(?P<cat>Character|Event|Stage)s?(?: cards?)?|cards?)",
            r"(?: with the <(?P<attr>[^>]+)> attribute)?",
            r#"(?: with a type including "(?P<inc>[^"]+)")?"#,
            r"(?P<trig> with a \[Trigger\])?",
            r"(?: with a cost of (?P<c>\d+)(?: or (?P<dir>more|less))?)?",
            r"(?: and the <(?P<attr2>[^>]+)> attribute)?",
            r#"(?: and a type including "(?P<inc2>[^"]+)")?"#,
            r"(?: with (?P<pw>\d+) power(?P<pwle> or less)?)?",
            r"(?P<trig2> and a \[Trigger\])?",
            r"(?: other than \[(?P<excl>[^\]]+)\])?"
        ),
        f,
    ) else {
        return either_filter(f);
    };
    Ok(Filter::General {
        color: g(&m, "color").map(String::from),
        types: find_all(r"\{([^}]+)\}", g(&m, "types").unwrap_or("")),
        cat: g(&m, "cat").and_then(Category::parse),
        inc: g(&m, "inc").or_else(|| g(&m, "inc2")).map(String::from),
        trig: g(&m, "trig").is_some() || g(&m, "trig2").is_some(),
        cost: g(&m, "c").map(|c| (c.parse().unwrap(), cost_dir(g(&m, "dir")))),
        power: g(&m, "pw").map(|p| (p.parse().unwrap(), g(&m, "pwle").is_some())),
        attr: g(&m, "attr").or_else(|| g(&m, "attr2")).map(String::from),
        excl: g(&m, "excl").map(String::from),
    })
}

// "[Megalo] or {Neptunian} type cards", "Event or {Punk Hazard} type cards": a bare name, category
// or type, or a second kind of card; a trailing "other than [X]" covers both. Other qualifiers can't
// be split safely ("yellow Character or Stage card with a cost of 2 or less"), so they stay unsupported.
const EITHER_LEFT: &str = r"\[[^\]]+\]|(?:Character|Event|Stage)s?(?: cards?)?|\{[^}]+\} type cards?";
const EITHER_SHARED: &str = r" with a cost of | power| attribute|^(?:red|green|blue|purple|black|yellow) ";

fn either_filter(f: &str) -> Res<Filter> {
    let m = rfull(&format!(r"(?P<left>{EITHER_LEFT}) or (?P<right>.+?)(?: other than \[(?P<excl>[^\]]+)\])?"), f)
        .ok_or(Unsupported)?;
    if re(EITHER_SHARED.to_string()).is_match(&m["right"]) {
        return Err(Unsupported);
    }
    Ok(Filter::Either {
        left: Box::new(parse_filter(&m["left"])?),
        right: Box::new(parse_filter(&m["right"])?),
        excl: g(&m, "excl").map(String::from),
    })
}

const OWN_PART: &str = concat!(
    r#"(?P<base>Leader or Character cards|Leader or Characters|Characters or \[[^\]]+\]|Leader|Characters?"#,
    r#"|(?P<types>\{[^}]+\}(?: or \{[^}]+\})*) type (?P<tl>Leader or )?Characters?|\[(?P<name>[^\]]+)\](?: cards?)?)"#,
    r#"(?: with a type including "(?P<inc>[^"]+)")?"#,
    r"(?: with both the \{(?P<both>[^}]+)\} type and a \[Trigger\])?",
    r"(?: with the <(?P<attr>[^>]+)>(?: or <(?P<attr2>[^>]+)>)? attribute)?",
    r"(?:(?: and| with) (?P<pw>\d+) power or less)?",
    r"(?: with a cost of (?P<cmin>\d+) or more)?"
);

/// Which of your cards a phrase allows: 'Leader or Characters', '[Nami] cards', ... With
/// `chars_only` the phrase must name only Characters (costs paid with them).
pub(crate) fn own_candidates(who: &str, chars_only: bool) -> Res<Vec<OwnPart>> {
    let mut parts = vec![];
    for text in re(r" or up to \d+ of your ".to_string()).split(who) {
        let m = rfull(OWN_PART, text).ok_or(Unsupported)?;
        let base = g(&m, "base").unwrap_or("");
        let types_s = g(&m, "types");
        let tl = g(&m, "tl").is_some();
        let names_chars = base.starts_with("Character") && !base.starts_with("Characters or");
        if chars_only && !(names_chars || (types_s.is_some() && !tl)) {
            return Err(Unsupported);
        }
        let pool = if base == "Leader" {
            Pool::Leader
        } else if (names_chars || types_s.is_some()) && !tl {
            Pool::Characters
        } else {
            Pool::LeaderAndCharacters
        };
        let orname = re(r"Characters or \[([^\]]+)\]".to_string()).captures(base).map(|c| c[1].to_string());
        parts.push(OwnPart {
            pool,
            types: find_all(r"\{([^}]+)\}", types_s.unwrap_or("")),
            name: g(&m, "name").map(String::from),
            orname,
            inc: g(&m, "inc").map(String::from),
            both: g(&m, "both").map(String::from),
            attrs: [g(&m, "attr"), g(&m, "attr2")].into_iter().flatten().map(String::from).collect(),
            power_max: g(&m, "pw").map(|p| p.parse().unwrap()),
            cost_min: g(&m, "cmin").map(|c| c.parse().unwrap()),
        });
    }
    Ok(parts)
}

// ---------------------------------------------------------------- templates

type Build = fn(&Captures) -> Res<Body>;

fn t(x: Tpl) -> Res<Body> {
    Ok(Body::T(x))
}

fn kw_of(m: &Captures, name: &str) -> Res<Kw> {
    Kw::printed(g(m, name).unwrap_or("")).ok_or(Unsupported)
}

fn templates() -> Vec<(String, Build)> {
    let who = r"(?P<who>.+?)";
    vec![
        (format!(r"K\.O\. up to {OPP}\."), |m| t(Tpl::Ko { n: gu(m, "n"), pred: opp_pred(m) })),
        (r"K\.O\. up to (?P<n>\d+) of your opponent's Stages\.".into(), |m| t(Tpl::KoStage { n: gu(m, "n") })),
        (format!(r"Rest up to {OPP}\."), |m| t(Tpl::Rest { n: gu(m, "n"), pred: opp_pred(m) })),
        (r"Rest all of your opponent's Characters\.".into(), |_| t(Tpl::RestAll)),
        (format!(r"Up to {OPP} cannot attack until the end of your opponent's next End Phase\."), |m| {
            t(Tpl::CannotAttack { n: gu(m, "n"), pred: opp_pred(m) })
        }),
        (format!(r"Up to {OPP} will not become active in your opponent's next Refresh Phase\."), |m| {
            t(Tpl::SkipRefresh { n: gu(m, "n"), pred: opp_pred(m) })
        }),
        (format!(r"Negate the effect of up to {OPP} during this turn(?P<ret>, and return it to the owner's hand)?\."), |m| {
            t(Tpl::Negate { n: gu(m, "n"), pred: opp_pred(m), back: g(m, "ret").is_some() })
        }),
        (format!(r"K\.O\. or rest up to {OPP}\."), |m| t(Tpl::KoOrRest { n: gu(m, "n"), pred: opp_pred(m) })),
        (
            concat!(
                r"Rest up to (?P<n>\d+) of your opponent's Characters with a cost equal to or less than the total of your ",
                r"and your opponent's Life cards\."
            )
            .into(),
            |m| t(Tpl::RestByLife { n: gu(m, "n") }),
        ),
        (
            concat!(
                r"Give up to (?P<n>\d+) of your opponent's (?P<rested>rested )?(?P<who>Leader or Character cards|",
                r"Leader or Characters|Characters)(?: with (?P<minpw>\d+) power or more)? -(?P<v>\d+) (?P<what>power|cost) ",
                r"(?:during this turn|until the end of your opponent's (?P<next>next) End Phase)\."
            )
            .into(),
            |m| {
                t(Tpl::Debuff {
                    n: gu(m, "n"),
                    v: gi(m, "v"),
                    cost: &m["what"] == "cost",
                    with_leader: m["who"].starts_with("Leader"),
                    rested: g(m, "rested").is_some(),
                    min_power: g(m, "minpw").map(|p| p.parse().unwrap()),
                    until: if g(m, "next").is_some() { Until::NextEnd } else { Until::Turn },
                })
            },
        ),
        (r"Give up to (?P<n>\d+) Characters? (?P<sign>[+-])(?P<v>\d+) power during this turn\.".into(), |m| {
            let sign = if &m["sign"] == "+" { 1 } else { -1 };
            t(Tpl::GiveAny { n: gu(m, "n"), v: sign * gi(m, "v"), label: format!("{}{} power", &m["sign"], &m["v"]) })
        }),
        (r"Give your opponent's Leader -(?P<v>\d+) power during this turn\.".into(), |m| {
            t(Tpl::OppLeaderDebuff { v: gi(m, "v") })
        }),
        (r"K\.O\. up to (?P<n>\d+) of your (?P<who>.+?)\.".into(), |m| {
            t(Tpl::KoOwn { n: gu(m, "n"), who: own_candidates(&m["who"], true)? })
        }),
        (
            concat!(
                r"All of your Characters with (?P<bp>\d+) base power or less gain \+(?P<v>\d+) power ",
                r"(?:during this (?P<dur>battle|turn)|until the end of your opponent's next End Phase)\."
            )
            .into(),
            |m| t(Tpl::BuffAll { bp: gi(m, "bp"), v: gi(m, "v"), until: until_from(g(m, "dur")) }),
        ),
        (
            concat!(
                r"All of your Characters with (?P<bp>\d+) base power and the \{(?P<t>[^}]+)\} type cannot be K\.O\.'d in ",
                r"battle until the end of your opponent's next End Phase\."
            )
            .into(),
            |m| t(Tpl::ProtectAll { bp: gi(m, "bp"), t: m["t"].to_string() }),
        ),
        (
            r"All of your opponent's Characters with a cost of (?P<c>\d+) cannot activate \[Blocker\] during this turn\."
                .into(),
            |m| t(Tpl::NoBlockerAll { cost: gi(m, "c") }),
        ),
        (
            r"Give all of your opponent's Characters -(?P<v>\d+) (?P<what>power|cost) during this turn\.".into(),
            |m| t(Tpl::DebuffAll { v: gi(m, "v"), cost: &m["what"] == "cost" }),
        ),
        (
            concat!(
                r"Return up to (?P<n>\d+) (?P<opp>of your opponent's )?Characters?",
                r"(?: with a cost of (?P<cost>\d+) or less| with a \[Trigger\](?P<trig>))? to the owner's (?P<where>hand)\."
            )
            .into(),
            bounce,
        ),
        (r"K\.O\. your opponent's Characters with a total cost of (?P<t>\d+) or less\.".into(), |m| {
            t(Tpl::KoTotal { budget: gi(m, "t") })
        }),
        (
            concat!(
                r"Place all of your opponent's (?P<n>)(?P<rested>)Characters with a (?P<basec>base )?cost of (?P<cost>\d+)",
                r"(?P<orless> or less)?(?P<pow>)(?P<basep>) at the bottom of the owner's deck",
                r"(?: in any order(?: of the owner's choosing)?)?\."
            )
            .into(),
            |m| t(Tpl::BottomAll { pred: opp_pred(m) }),
        ),
        (r"Rest up to (?P<n>\d+) of your opponent's Leader or Characters\.".into(), |m| {
            t(Tpl::RestLeaderOrChar { n: gu(m, "n") })
        }),
        (r"You cannot play Character cards during this turn\.".into(), |_| t(Tpl::NoCharacterPlay)),
        (
            concat!(
                r"Look at (?P<n>\d+) cards? from the top of your deck, reorganize them in any order and place them at ",
                r"the top or bottom of your deck\."
            )
            .into(),
            |m| t(Tpl::Scry { n: gu(m, "n") }),
        ),
        (
            concat!(
                r"Place up to (?P<n>\d+) (?P<opp>of your opponent's )?Characters? with a cost of (?P<cost>\d+) or less ",
                r"at the bottom of the owner's (?P<where>deck)\."
            )
            .into(),
            bounce,
        ),
        (r"Draw (?P<d>\d+) cards? and trash (?P<t>\d+) cards? from your hand\.".into(), |m| {
            t(Tpl::DrawTrash { d: gu(m, "d"), t: gu(m, "t") })
        }),
        (r"Draw (?P<d>\d+) cards?(?:\.|,? and )".into(), |m| t(Tpl::Draw { d: gu(m, "d") })),
        (r"Trash (?P<t>\d+) cards? from your hand\.".into(), |m| t(Tpl::TrashHand { t: gu(m, "t") })),
        (r"(?:Your opponent trashes|They trash) (?P<n>\d+) cards? from their hand\.".into(), |m| {
            t(Tpl::OppTrashes { n: gu(m, "n") })
        }),
        (r"Trash (?P<n>\d+) cards? from your opponent's hand\.".into(), |m| t(Tpl::TrashOppHand { n: gu(m, "n") })),
        (
            r"Your opponent places (?P<n>\d+) cards? from their hand at the bottom of their deck(?: in any order)?\.".into(),
            |m| t(Tpl::OppBottoms { n: gu(m, "n") }),
        ),
        (r"Trash (?P<t>\d+) cards? from the top of your deck\.".into(), |m| t(Tpl::Mill { n: gu(m, "t") })),
        (
            concat!(
                r"Add (?:up to )?(?P<n>\d+) cards? from the top of your (?P<opp>opponent's )?Life cards to ",
                r"(?:your|the owner's) hand\."
            )
            .into(),
            |m| t(Tpl::LifeToHand { n: gu(m, "n"), opp: g(m, "opp").is_some() }),
        ),
        (r"Add up to (?P<n>\d+) cards? from the top of your deck to the top of your Life cards\.".into(), |m| {
            t(Tpl::DeckToLife { n: gu(m, "n") })
        }),
        (
            r"Add up to (?P<n>\d+) DON!! cards? from your DON!! deck and (?P<how>set (?:it|them) as active|rest (?:it|them))\."
                .into(),
            |m| t(Tpl::AddDon { n: gi(m, "n") as u32, rested: m["how"].starts_with("rest") }),
        ),
        (r"Add up to (?P<n>\d+) DON!! cards? as (?P<how>active|rested) from your DON!! deck\.".into(), |m| {
            t(Tpl::AddDon { n: gi(m, "n") as u32, rested: m["how"].starts_with("rest") })
        }),
        (r"Set up to (?P<n>\d+) of your DON!! cards as active\.".into(), |m| {
            t(Tpl::SetDonActive { n: gi(m, "n") as u32 })
        }),
        (
            concat!(
                r"Give up to (?P<n>\d+) rested DON!! cards? to your (?:\[(?P<name>[^\]]+)\] )?Leader",
                r"(?P<or> or \d+ of your Characters)?\."
            )
            .into(),
            |m| {
                t(Tpl::GiveRestedDon {
                    n: gi(m, "n") as u32,
                    chars: g(m, "or").is_some(),
                    name: g(m, "name").map(String::from),
                })
            },
        ),
        (r"Give up to (?P<n>\d+) rested DON!! cards? to 1 of your (?P<who>.+?)\.".into(), |m| {
            t(Tpl::GiveRestedDonTo { n: gi(m, "n") as u32, who: own_candidates(&m["who"], false)? })
        }),
        (format!(r"Set up to (?P<n>\d+) of your {who} as active\."), |m| {
            t(Tpl::SetActive { n: gu(m, "n"), who: own_candidates(&m["who"], false)? })
        }),
        (format!(r"Up to (?P<n>\d+) of your {who} gains? \+(?P<v>\d+) power during this (?P<dur>battle|turn)\."), |m| {
            t(Tpl::Buff {
                n: gu(m, "n"),
                v: gi(m, "v"),
                until: until_from(g(m, "dur")),
                who: own_candidates(&m["who"], false)?,
            })
        }),
        (format!(r"Up to (?P<n>\d+) of your {who} gains? \[(?P<kw>[^\]]+)\] during this turn\."), |m| {
            let kw = kw_of(m, "kw")?;
            t(Tpl::Grant { n: gu(m, "n"), kw, who: own_candidates(&m["who"], false)? })
        }),
        (
            concat!(
                r"Your (?:(?P<types>\{[^}]+\}(?: or \{[^}]+\})*) type )?Leader gains \+(?P<v>\d+) power during this ",
                r"(?P<dur>battle|turn)\."
            )
            .into(),
            |m| {
                t(Tpl::LeaderBuff {
                    v: gi(m, "v"),
                    until: until_from(g(m, "dur")),
                    types: find_all(r"\{([^}]+)\}", g(m, "types").unwrap_or("")),
                })
            },
        ),
        (r"Your (?:\[(?P<name>[^\]]+)\] )?Leader gains \[(?P<kw>[^\]]+)\] during this turn\.".into(), |m| {
            t(Tpl::LeaderKeyword { kw: kw_of(m, "kw")?, name: g(m, "name").map(String::from) })
        }),
        (
            concat!(
                r"Your (?:(?P<who>\[[^\]]+\]|\{[^}]+\} type|monocolored) )?Leader's base power becomes (?P<v>\d+) ",
                r"(?:until the end of your opponent's next End Phase|during this (?P<dur>turn))\."
            )
            .into(),
            |m| {
                t(Tpl::LeaderBasePower {
                    v: gi(m, "v"),
                    who: g(m, "who").unwrap_or("").to_string(),
                    until: if g(m, "dur").is_some() { Until::Turn } else { Until::NextEnd },
                })
            },
        ),
        (
            concat!(
                r"This (?:Character|Leader|card) gains (?:\[(?P<kw>[^\]]+)\])?(?: and )?(?:\+(?P<v>\d+) power)? ",
                r"(?:during this (?P<dur>battle|turn)|until the end of your opponent's next End Phase)\."
            )
            .into(),
            |m| {
                let kw = match g(m, "kw") {
                    Some(k) => Some(Kw::printed(k).ok_or(Unsupported)?),
                    None => None,
                };
                let v = g(m, "v").map_or(0, |v| v.parse().unwrap());
                if kw.is_none() && v == 0 {
                    return Err(Unsupported);
                }
                t(Tpl::SelfGain { kw, v, until: until_from(g(m, "dur")) })
            },
        ),
        (r"Your opponent cannot activate \[Blocker\] during this battle\.".into(), |_| t(Tpl::Unblockable)),
        (r"Play this (?:Character )?card(?: from your trash)?\.".into(), |_| t(Tpl::PlaySelf)),
        (r"Activate this card's \[(?P<t>[^\]]+)\] effect\.".into(), |m| {
            t(Tpl::ActivateOwn { timing: Timing::from_tag(&m["t"]).ok_or(Unsupported)? })
        }),
        (
            r"Look at (?P<n>\d+) cards? from the top of your deck and place them at the top or bottom of the deck in any order\."
                .into(),
            |m| t(Tpl::Scry { n: gu(m, "n") }),
        ),
        (
            concat!(
                r"Look at (?P<n>\d+) cards? from the top of your deck; reveal up to (?:a total of )?(?P<k>\d+) (?P<f>.+?) and ",
                r"add (?:it|them) to your hand(?:\. Then,|, then) ",
                r"(?P<rest>place the rest at the bottom of your deck in any order|trash the rest)\."
            )
            .into(),
            |m| {
                t(Tpl::Look {
                    n: gu(m, "n"),
                    k: gu(m, "k"),
                    pred: parse_filter(&m["f"])?,
                    to_trash: m["rest"].starts_with("trash"),
                })
            },
        ),
        (
            r"Play up to (?P<n>\d+) (?P<f>[^.]+?) from your (?P<zone>hand or trash|hand|trash)(?P<rested> rested)?\.".into(),
            |m| {
                let zone = &m["zone"];
                t(Tpl::PlayFrom {
                    n: gu(m, "n"),
                    hand: zone.contains("hand"),
                    trash: zone.contains("trash"),
                    hand_first: true,
                    rested: g(m, "rested").is_some(),
                    pred: parse_filter(&m["f"])?,
                })
            },
        ),
        (r"Add up to (?P<n>\d+) (?P<f>[^.]+?) from your trash to your hand\.".into(), |m| {
            t(Tpl::TrashToHand { n: gu(m, "n"), pred: parse_filter(&m["f"])? })
        }),
        (
            concat!(
                r"(?:Add|Place) up to (?P<n>\d+) (?P<opp>of your opponent's )?Characters? with a cost of (?P<cost>\d+) or less ",
                r"(?:to|at) the top or bottom of (?:the owner's|your opponent's|their) Life cards face-up\."
            )
            .into(),
            |m| t(Tpl::CharToLife { n: gu(m, "n"), opp_only: g(m, "opp").is_some(), cost: gi(m, "cost") }),
        ),
        (r"Add up to (?P<n>\d+) (?P<f>[^.]+?) from your hand to the top of your Life cards(?P<fu> face-up)?\.".into(), |m| {
            t(Tpl::HandToLife { n: gu(m, "n"), pred: parse_filter(&m["f"])?, face_up: g(m, "fu").is_some() })
        }),
    ]
}

fn bounce(m: &Captures) -> Res<Body> {
    t(Tpl::Bounce {
        n: gu(m, "n"),
        opp_only: g(m, "opp").is_some(),
        to: if &m["where"] == "deck" { Where::Deck } else { Where::Hand },
        cost: g(m, "cost").map(|c| c.parse().unwrap()),
        // Python reads this empty group as False, so the [Trigger] restriction never applies.
        // Kept identical to engine.py until it's fixed in both.
        trig: g(m, "trig").is_some(),
    })
}
