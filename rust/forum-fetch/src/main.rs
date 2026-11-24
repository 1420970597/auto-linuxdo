//! Fetches one page through cfsolve's browser profile, clearing Cloudflare challenges.
//!
//! Prints a JSON document on stdout:
//! `{"url": .., "status": 200, "html": "..", "cookies": {"name": "value"}, "solved": false}`
//!
//! Cookies given with `--cookie-file` are loaded into the request jar first, and the jar is
//! written back to the same file so a `cf_clearance` cookie can be reused across processes.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::ExitCode;

use cfsolve::fingerprint::Transport;
use cfsolve::http::{PageDocument, navigate};
use cfsolve::{Browser, Options};
use serde::Serialize;
use url::Url;

const USAGE: &str = "usage: forum-fetch --profile <file> --url <url> [--cookie-file <file>] [--proxy <url>]";

#[derive(Serialize)]
struct Output {
    url: String,
    status: u16,
    html: String,
    cookies: BTreeMap<String, String>,
    solved: bool,
}

struct Args {
    profile: PathBuf,
    url: Url,
    cookie_file: Option<PathBuf>,
    proxy: Option<String>,
}

fn parse_args() -> Result<Args, String> {
    let mut profile = None;
    let mut url = None;
    let mut cookie_file = None;
    let mut proxy = None;
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let mut value = || args.next().ok_or_else(|| format!("{flag} needs a value"));
        match flag.as_str() {
            "--profile" => profile = Some(PathBuf::from(value()?)),
            "--url" => {
                let raw = value()?;
                url = Some(Url::parse(&raw).map_err(|_| format!("invalid --url: {raw}"))?);
            }
            "--cookie-file" => cookie_file = Some(PathBuf::from(value()?)),
            "--proxy" => proxy = Some(value()?),
            "-h" | "--help" => return Err(USAGE.to_string()),
            other => return Err(format!("unknown argument: {other}\n{USAGE}")),
        }
    }
    Ok(Args {
        profile: profile.ok_or(USAGE)?,
        url: url.ok_or(USAGE)?,
        cookie_file,
        proxy,
    })
}

fn read_cookies(path: &PathBuf) -> BTreeMap<String, String> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn write_cookies(path: &PathBuf, cookies: &BTreeMap<String, String>) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, serde_json::to_vec(cookies)?)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

/// A Cloudflare interstitial, which carries none of the page's own markup.
fn challenged(page: &PageDocument) -> bool {
    let status = page.status.as_u16();
    (status == 403 || status == 503)
        && ["challenge-platform", "_cf_chl_opt", "cf-chl-widget", "Just a moment"]
            .iter()
            .any(|marker| page.html.contains(marker))
}

fn store_cookies(client: &cfsolve::http::BrowserClient, url: &Url) -> BTreeMap<String, String> {
    client
        .cookies(url)
        .into_iter()
        .map(|cookie| (cookie.name().to_string(), cookie.value().to_string()))
        .collect()
}

fn load_cookies(client: &cfsolve::http::BrowserClient, url: &Url, cookies: &BTreeMap<String, String>) {
    let host = url.host_str().unwrap_or_default();
    for (name, value) in cookies {
        client.add_cookie(
            &format!("{name}={value}; Domain={host}; Path=/; Secure"),
            url,
        );
    }
}

/// Navigates once, then once more: the first request after a challenge can fail in transit.
async fn fetch_page(client: &cfsolve::http::BrowserClient, url: &Url) -> Result<PageDocument, String> {
    match navigate(client, url).await {
        Ok(page) => Ok(page),
        Err(first) => navigate(client, url)
            .await
            .map_err(|second| format!("{first:#}; retry: {second:#}")),
    }
}

fn run(args: &Args) -> Result<Output, String> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())?;
    runtime.block_on(async {
        let transport = Transport::read(&args.profile).map_err(|error| error.to_string())?;
        let client = transport
            .client(args.proxy.as_deref(), None)
            .map_err(|error| error.to_string())?;

        let saved = args
            .cookie_file
            .as_ref()
            .map(read_cookies)
            .unwrap_or_default();
        load_cookies(&client, &args.url, &saved);

        let mut solved = false;
        let mut page = fetch_page(&client, &args.url).await?;

        if challenged(&page) {
            let browser = Browser::read(&args.profile).map_err(|error| error.to_string())?;
            let options = Options {
                proxy: args.proxy.clone(),
                ..Options::default()
            };
            let clearance = match browser.clear(&args.url, &options).await {
                Ok(clearance) => Some(clearance),
                // Someone else already cleared it; navigating again is enough.
                Err(cfsolve::Error::NotChallenged) => None,
                Err(error) => return Err(format!("Cloudflare solve failed: {error}")),
            };
            if let Some(clearance) = clearance {
                solved = true;
                let host = args.url.host_str().unwrap_or_default();
                for cookie in &clearance.cookies {
                    client.add_cookie(
                        &format!(
                            "{}={}; Domain={host}; Path=/; Secure",
                            cookie.name(),
                            cookie.value()
                        ),
                        &args.url,
                    );
                }
            }
            page = fetch_page(&client, &args.url).await?;
            if challenged(&page) {
                return Err("Cloudflare challenge could not be cleared".to_string());
            }
        }

        let cookies = store_cookies(&client, &args.url);
        if let Some(path) = &args.cookie_file {
            write_cookies(path, &cookies).map_err(|error| error.to_string())?;
        }

        Ok(Output {
            url: page.url.to_string(),
            status: page.status.as_u16(),
            html: page.html,
            cookies,
            solved,
        })
    })
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(args) => args,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::from(2);
        }
    };
    match run(&args) {
        Ok(output) => {
            match serde_json::to_string(&output) {
                Ok(json) => println!("{json}"),
                Err(error) => {
                    eprintln!("{error}");
                    return ExitCode::FAILURE;
                }
            }
            ExitCode::SUCCESS
        }
        Err(message) => {
            let error = serde_json::json!({"error": message});
            println!("{error}");
            ExitCode::FAILURE
        }
    }
}
