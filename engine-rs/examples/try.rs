//! Debug the second compiler: `cargo run --example try -- "sentence"` (or `-- sel "cards"`).
fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    if a.first().map(String::as_str) == Some("line") {
        println!("{}", optcg_rs::compile2::debug_line(&a[1]));
    } else if a.first().map(String::as_str) == Some("sel") {
        println!("{}", optcg_rs::compile2::debug_sel(&a[1]));
    } else {
        println!("{}", optcg_rs::compile2::debug(&a.join(" ")));
    }
}
