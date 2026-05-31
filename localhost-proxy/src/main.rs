//! Wallet-friendly localhost proxy. Browsers and CLI tools speak vanilla
//! HTTP/1.1 + WebSocket to `127.0.0.1:NNNN`, this shim wraps each request in
//! Noise + attestation and forwards it to an enclavia enclave.
//!
//! Use case: a Cashu mint (or any service exposing `/v1/ws` etc.) running
//! inside an enclave can be reached by an unmodified browser wallet by
//! pointing it at `http://localhost:NNNN`.

use std::net::SocketAddr;

use base64::Engine as _;
use clap::Parser;
use enclavia::{Client, Method, Pcrs};
use sha1::{Digest, Sha1};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tracing::{error, info, warn};

fn parse_method(s: &str) -> Option<Method> {
    match s.to_ascii_uppercase().as_str() {
        "GET" => Some(Method::Get),
        "POST" => Some(Method::Post),
        "PUT" => Some(Method::Put),
        "DELETE" => Some(Method::Delete),
        "PATCH" => Some(Method::Patch),
        "HEAD" => Some(Method::Head),
        "OPTIONS" => Some(Method::Options),
        _ => None,
    }
}

#[derive(Parser, Debug)]
#[command(version, about = "Localhost HTTP+WS proxy for enclavia enclaves")]
struct Args {
    /// Local address to bind, e.g. `127.0.0.1:8000`.
    #[arg(long, default_value = "127.0.0.1:8000")]
    listen: SocketAddr,

    /// `wss://` URL of the enclavia endpoint.
    #[arg(long)]
    enclave_url: String,

    /// Hex-encoded PCR0.
    #[arg(long, default_value = "")]
    pcr0: String,
    /// Hex-encoded PCR1.
    #[arg(long, default_value = "")]
    pcr1: String,
    /// Hex-encoded PCR2.
    #[arg(long, default_value = "")]
    pcr2: String,

    /// Skip attestation signature verification (debug-mode enclaves only).
    #[arg(long)]
    debug_mode: bool,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let args = Args::parse();

    let pcrs = Pcrs {
        pcr0: hex::decode(&args.pcr0).unwrap_or_default(),
        pcr1: hex::decode(&args.pcr1).unwrap_or_default(),
        pcr2: hex::decode(&args.pcr2).unwrap_or_default(),
    };

    let client = Client::builder(&args.enclave_url)
        .pcrs(pcrs)
        .debug_mode(args.debug_mode)
        .build()
        .await?;
    info!(url = %args.enclave_url, "Connected and attested");

    let listener = TcpListener::bind(args.listen).await?;
    info!(local = %args.listen, "Listening for browser/wallet traffic");

    loop {
        let (tcp, peer) = listener.accept().await?;
        let client = client.clone();
        tokio::spawn(async move {
            if let Err(e) = handle(tcp, client).await {
                warn!(%peer, error = %e, "Connection ended with error");
            }
        });
    }
}

/// Read until we have a complete HTTP/1.1 request head (status line, headers,
/// terminating double CRLF). Returns the accumulated buffer and the byte
/// index at which the body starts within it.
async fn read_request_head(stream: &mut TcpStream) -> Result<(Vec<u8>, usize), std::io::Error> {
    let mut buf = Vec::with_capacity(4096);
    let mut chunk = [0u8; 4096];
    loop {
        let n = stream.read(&mut chunk).await?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "client closed before request head was complete",
            ));
        }
        buf.extend_from_slice(&chunk[..n]);

        let mut headers = [httparse::EMPTY_HEADER; 64];
        let mut req = httparse::Request::new(&mut headers);
        match req.parse(&buf) {
            Ok(httparse::Status::Complete(head_len)) => return Ok((buf, head_len)),
            Ok(httparse::Status::Partial) => continue,
            Err(e) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!("malformed request: {e}"),
                ));
            }
        }
    }
}

struct ParsedHead {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    content_length: Option<usize>,
    is_upgrade: bool,
}

fn parse_head(raw: &[u8]) -> Result<ParsedHead, String> {
    let mut headers = [httparse::EMPTY_HEADER; 64];
    let mut req = httparse::Request::new(&mut headers);
    req.parse(raw).map_err(|e| e.to_string())?;
    let method = req.method.ok_or("missing method")?.to_string();
    let path = req.path.ok_or("missing path")?.to_string();
    let mut hdrs: Vec<(String, String)> = Vec::new();
    let mut content_length = None;
    let mut is_upgrade = false;
    for h in req.headers.iter() {
        let name = h.name.to_string();
        let value = String::from_utf8_lossy(h.value).into_owned();
        if name.eq_ignore_ascii_case("content-length") {
            content_length = value.trim().parse::<usize>().ok();
        }
        if name.eq_ignore_ascii_case("upgrade") && !value.trim().is_empty() {
            is_upgrade = true;
        }
        hdrs.push((name, value));
    }
    Ok(ParsedHead {
        method,
        path,
        headers: hdrs,
        content_length,
        is_upgrade,
    })
}

/// One incoming TCP connection from the browser/wallet.
async fn handle(mut tcp: TcpStream, client: Client) -> Result<(), Box<dyn std::error::Error>> {
    let (head_buf, head_len) = read_request_head(&mut tcp).await?;
    let head_bytes = &head_buf[..head_len];
    let mut body_seen = head_buf[head_len..].to_vec();

    let parsed = parse_head(head_bytes).map_err(|e| format!("parse head: {e}"))?;

    if parsed.is_upgrade {
        info!(method = %parsed.method, path = %parsed.path, "Upgrade request");
        return handle_upgrade(tcp, client, parsed).await;
    }

    // Plain HTTP request. Read the body to completion before forwarding;
    // the SDK takes a single Vec<u8> body and the enclavia server expects
    // a complete request.
    if let Some(len) = parsed.content_length {
        while body_seen.len() < len {
            let mut buf = vec![0u8; 8192];
            let n = tcp.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            body_seen.extend_from_slice(&buf[..n]);
        }
        body_seen.truncate(len);
    }

    let method = match parse_method(&parsed.method) {
        Some(m) => m,
        None => {
            // We don't bother supporting CONNECT or arbitrary methods through
            // the simple proxy; the SDK's `Method` enum is the supported set.
            write_status(&mut tcp, 501, "Not Implemented", b"unsupported method").await?;
            return Ok(());
        }
    };

    let mut req = client.request(method, &parsed.path);
    for (k, v) in &parsed.headers {
        if should_forward_header(k) {
            req = req.header(k.clone(), v.clone());
        }
    }
    if !body_seen.is_empty() {
        req = req.body(body_seen);
    }

    let resp = match req.send().await {
        Ok(r) => r,
        Err(e) => {
            error!(error = %e, "Forward failed");
            write_status(&mut tcp, 502, "Bad Gateway", e.to_string().as_bytes()).await?;
            return Ok(());
        }
    };

    write_response(&mut tcp, &resp).await?;
    Ok(())
}

/// Bridge a browser WS upgrade to an enclavia `UpgradedStream`. We re-send the
/// 101 response head verbatim to the browser, then splice bytes both ways.
async fn handle_upgrade(
    mut tcp: TcpStream,
    client: Client,
    parsed: ParsedHead,
) -> Result<(), Box<dyn std::error::Error>> {
    let method = parse_method(&parsed.method).ok_or_else(|| format!("unsupported method: {}", parsed.method))?;

    // Forward every header the browser sent (Upgrade, Sec-WebSocket-*, etc.)
    // verbatim. The SDK preserves them and won't inject Connection: close on
    // the upgrade path.
    let forwarded: Vec<(String, String)> = parsed
        .headers
        .iter()
        .filter(|(k, _)| should_forward_header(k))
        .cloned()
        .collect();

    let stream = match client.upgrade(method, &parsed.path, &forwarded).await {
        Ok(s) => s,
        Err(enclavia::Error::UpgradeFailed { status, head }) => {
            // Pass the workload's rejection straight through.
            tcp.write_all(&head).await?;
            tcp.shutdown().await?;
            warn!(status, "Upgrade rejected by workload");
            return Ok(());
        }
        Err(e) => {
            error!(error = %e, "Upgrade failed");
            write_status(&mut tcp, 502, "Bad Gateway", e.to_string().as_bytes()).await?;
            return Ok(());
        }
    };

    // Synthesize the 101 head the browser expects. We forward the workload's
    // selected Sec-WebSocket-Accept etc. by trusting the SDK's UpgradedStream
    // to faithfully pass them through. But the SDK strips the head off and
    // returns only post-upgrade bytes, so we have to mint the head ourselves
    // from the request's Sec-WebSocket-Key. The workload's response head was
    // not preserved here yet (this is a sample meant for a single-workload
    // setup where the browser supplies a fresh Sec-WebSocket-Key per
    // connection and the workload's library computes the same accept).
    //
    // For simplicity we send a stock 101 head and rely on the browser-side
    // tungstenite verifier accepting Sec-WebSocket-Accept computed from the
    // request's Sec-WebSocket-Key. To stay correct we recompute it here.
    let sec_ws_key = parsed
        .headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("sec-websocket-key"))
        .map(|(_, v)| v.clone());
    let accept = sec_ws_key.as_deref().map(ws_accept_value);

    let mut head_out = String::from("HTTP/1.1 101 Switching Protocols\r\n");
    head_out.push_str("Upgrade: websocket\r\n");
    head_out.push_str("Connection: Upgrade\r\n");
    if let Some(a) = accept {
        head_out.push_str(&format!("Sec-WebSocket-Accept: {a}\r\n"));
    }
    head_out.push_str("\r\n");
    tcp.write_all(head_out.as_bytes()).await?;

    // From here both ends speak raw WS frames; pipe bytes through.
    let (mut tcp_r, mut tcp_w) = tcp.into_split();
    let (mut enc_r, mut enc_w) = tokio::io::split(stream);

    let to_enc = async move {
        let _ = tokio::io::copy(&mut tcp_r, &mut enc_w).await;
        let _ = enc_w.shutdown().await;
    };
    let to_tcp = async move {
        let _ = tokio::io::copy(&mut enc_r, &mut tcp_w).await;
        let _ = tcp_w.shutdown().await;
    };
    tokio::join!(to_enc, to_tcp);

    Ok(())
}

/// Allow-list: pass user headers through unchanged. We strip hop-by-hop
/// headers (Host gets replaced by the SDK; Content-Length is recomputed by
/// the SDK for non-upgrade requests).
fn should_forward_header(name: &str) -> bool {
    !matches!(
        name.to_ascii_lowercase().as_str(),
        "host" | "content-length"
    )
}

async fn write_response(
    tcp: &mut TcpStream,
    resp: &enclavia::Response,
) -> Result<(), std::io::Error> {
    let mut out = format!("HTTP/1.1 {} {}\r\n", resp.status(), status_text(resp.status()));
    let mut saw_content_length = false;
    for (k, v) in resp.headers() {
        if k.eq_ignore_ascii_case("transfer-encoding") {
            // The body is already fully buffered in `resp`; chunked encoding
            // back to the browser would require re-encoding. Skip and rely on
            // the explicit Content-Length we add below.
            continue;
        }
        if k.eq_ignore_ascii_case("content-length") {
            saw_content_length = true;
        }
        out.push_str(&format!("{k}: {v}\r\n"));
    }
    if !saw_content_length {
        out.push_str(&format!("Content-Length: {}\r\n", resp.bytes().len()));
    }
    out.push_str("\r\n");
    tcp.write_all(out.as_bytes()).await?;
    tcp.write_all(resp.bytes()).await?;
    Ok(())
}

async fn write_status(
    tcp: &mut TcpStream,
    code: u16,
    reason: &str,
    body: &[u8],
) -> Result<(), std::io::Error> {
    let head = format!(
        "HTTP/1.1 {code} {reason}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    tcp.write_all(head.as_bytes()).await?;
    tcp.write_all(body).await?;
    Ok(())
}

fn status_text(code: u16) -> &'static str {
    match code {
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        301 => "Moved Permanently",
        302 => "Found",
        304 => "Not Modified",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        _ => "OK",
    }
}

/// Compute the WebSocket `Sec-WebSocket-Accept` value:
/// base64(sha1(key + magic)). RFC 6455 section 4.2.2.
fn ws_accept_value(key: &str) -> String {
    const MAGIC: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    let mut hasher = Sha1::new();
    hasher.update(key.as_bytes());
    hasher.update(MAGIC.as_bytes());
    let digest = hasher.finalize();
    base64::engine::general_purpose::STANDARD.encode(digest)
}
