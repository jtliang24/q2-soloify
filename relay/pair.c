/* Pairing by code (docs/wire.md, Pairing), the relay's side: for the Soloify app's Pair level
 * (manage.c), and for a relay with no remote (main.c), which shows the code as its page's error
 * message. A run looks for the bridge by UDP broadcast, or dials the address the user typed;
 * connects over TLS without a pin, keeping the certificate's hash F; sends pair 1 NONCE NAME; shows
 * the code of F and the nonce; and, once the user has typed that code on the server, stores the
 * remote lines the bridge answers with, each pinned to F. An impostor's certificate gives another
 * code, so a code the server accepts proves the link.
 *
 * Away (away_round, away_state): whether the WAN line has worked from the device.
 *
 * For tests: SOLOIFY_PAIR_BROADCAST (one IPv4 address in place of the interfaces' broadcast
 * addresses), SOLOIFY_PAIR_UDP_PORT (7444), SOLOIFY_NAME (the name sent: Q2, or M1 from card/run)
 * and SOLOIFY_AWAY_COUNT (/tmp/q2-soloify.away). */
#include "relay.h"

#define UDP_PORT 7444
#define SENDS 3
#define SEND_GAP_MS 1000 /* three datagrams over 2 s */
#define LOOK_MS 10000    /* then the address is asked for */
#define ANSWER_MS 200000 /* the bridge holds a request 3 minutes: at least 190 s */
#define AWAY_COUNT "/tmp/q2-soloify.away"
#define UNREACHED 3 /* WAN failures in a row, no LAN success between them */
#define ANSWER_MAX 4096

struct pair pair;

static int stopped(void) { return quitting || pair.cancel; }

/* src into dst of cap bytes, cut to fit */
static void copy(char *dst, int cap, const char *src) {
    int n = strlen(src);
    if (n > cap - 1) n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = 0;
}

/* relay_wait, a quarter second at a time, until quitting or a cancel; the relay's own remote
 * appearing (a file copied by hand, the page's Retry) cancels too */
static int wait_for(int fd, short events, uint64_t deadline) {
    while (!stopped()) {
        uint64_t now = now_ms(), until = now + 250 < deadline ? now + 250 : deadline;
        int r = relay_wait(fd, events, until);
        if (r) return r;
        if (pair.watch && retry_remote) {
            struct endpoint ep;
            retry_remote = 0;
            if (remote_load(pair.watch, &ep, 1) > 0) pair.cancel = 1;
        }
        if (now_ms() >= deadline) return 0;
    }
    return 0;
}

static void hex(const unsigned char *b, int n, char *out) {
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < n; ++i) out[2 * i] = digits[b[i] >> 4], out[2 * i + 1] = digits[b[i] & 15];
    out[2 * n] = 0;
}

void pair_code(const unsigned char f[32], const unsigned char nonce[16], char out[8]) {
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned len = 0;
    uint32_t v = 0;
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    if (ctx && EVP_DigestInit_ex(ctx, EVP_sha256(), NULL) && EVP_DigestUpdate(ctx, "soloify-pair-1", 14) &&
        EVP_DigestUpdate(ctx, f, 32) && EVP_DigestUpdate(ctx, nonce, 16) && EVP_DigestFinal_ex(ctx, md, &len) &&
        len == 32)
        v = be32(md) % 1000000;
    if (ctx) EVP_MD_CTX_free(ctx);
    snprintf(out, 8, "%03u %03u", (unsigned)(v / 1000), (unsigned)(v % 1000));
}

/* The name sent with pair: SOLOIFY_NAME when it is 1-32 of [A-Za-z0-9._-], else Q2 */
static const char *name(void) {
    const char *s = getenv("SOLOIFY_NAME");
    int n = 0;
    for (; s && s[n]; ++n) {
        char c = s[n];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' || c == '_' ||
              c == '-'))
            return "Q2";
    }
    return n >= 1 && n <= 32 ? s : "Q2";
}

static int ipv4(const char *s, uint32_t *out) {
    unsigned char b[4];
    for (int i = 0; i < 4; ++i) {
        int v = 0, n = 0;
        for (; *s >= '0' && *s <= '9' && n < 4; ++s, ++n) v = v * 10 + *s - '0';
        if (!n || v > 255 || *s != (i < 3 ? '.' : 0)) return 0;
        b[i] = v;
        if (i < 3) ++s;
    }
    memcpy(out, b, 4); /* network order, as written */
    return 1;
}

static unsigned short udp_port(void) {
    const char *s = getenv("SOLOIFY_PAIR_UDP_PORT");
    long v = s ? strtol(s, NULL, 10) : 0;
    return v > 0 && v < 65536 ? v : UDP_PORT;
}

/* The datagram to each interface's broadcast address (not loopback's), or to 255.255.255.255 when
 * none has one */
static void broadcast(int fd, const char *msg, int len) {
    struct sockaddr_in to;
    struct ifaddrs *all, *i;
    const char *only = getenv("SOLOIFY_PAIR_BROADCAST");
    unsigned short port = udp_port();
    int sent = 0;
    memset(&to, 0, sizeof to);
    to.sin_family = AF_INET;
    to.sin_port = (unsigned short)((port >> 8) | (port << 8)); /* both targets are little-endian */
    if (only && *only) {
        if (ipv4(only, &to.sin_addr.s_addr)) sendto(fd, msg, len, 0, (struct sockaddr *)&to, sizeof to);
        return;
    }
    if (!getifaddrs(&all)) {
        for (i = all; i; i = i->ifa_next) {
            if (!i->ifa_addr || !i->ifa_broadaddr || (i->ifa_flags & IFF_LOOPBACK) || !(i->ifa_flags & IFF_UP) ||
                !(i->ifa_flags & IFF_BROADCAST))
                continue;
            if (((struct sockaddr_in *)i->ifa_addr)->sin_family != AF_INET) continue;
            to.sin_addr = ((struct sockaddr_in *)i->ifa_broadaddr)->sin_addr;
            sent += sendto(fd, msg, len, 0, (struct sockaddr *)&to, sizeof to) == len;
        }
        freeifaddrs(all);
    }
    if (!sent) {
        memset(&to.sin_addr, 0xff, 4);
        sendto(fd, msg, len, 0, (struct sockaddr *)&to, sizeof to);
    }
}

/* SOLOIFY-HERE 1 NONCE HOST PORT with our nonce: pair.host and pair.port */
static int here(char *buf, int n, const char *nonce) {
    char *p = buf + 15, *host, *port;
    if (n && buf[n - 1] == '\n') buf[--n] = 0;
    if (n < 15 + 32 + 4 || memcmp(buf, "SOLOIFY-HERE 1 ", 15) || memcmp(p, nonce, 32) || p[32] != ' ') return 0;
    host = p + 33;
    if (!(port = strchr(host, ' ')) || port == host || port - host >= (int)sizeof pair.host) return 0;
    for (char *c = host; c < port; ++c)
        if (!((*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') || (*c >= '0' && *c <= '9') || *c == '.' ||
              *c == '-' || *c == ':'))
            return 0;
    *port++ = 0;
    long v = 0;
    int digits = 0;
    for (char *c = port; *c; ++c, ++digits)
        if (*c < '0' || *c > '9' || digits > 5) return 0;
    if (!digits || (v = strtol(port, NULL, 10)) < 1 || v > 65535) return 0;
    copy(pair.host, sizeof pair.host, host);
    snprintf(pair.port, sizeof pair.port, "%ld", v);
    return 1;
}

static int discover(const char *nonce) {
    char msg[64], buf[256];
    int len = snprintf(msg, sizeof msg, "SOLOIFY-PAIR 1 %s", nonce), on = 1, found = 0, sent = 0, n;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    uint64_t start = now_ms(), next = start;
    if (fd < 0) return 0;
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &on, sizeof on);
    say("pair: looking for the bridge");
    while (!found && !stopped() && now_ms() < start + LOOK_MS) {
        if (sent < SENDS && now_ms() >= next) broadcast(fd, msg, len), ++sent, next += SEND_GAP_MS;
        if (wait_for(fd, POLLIN, sent < SENDS ? next : start + LOOK_MS) & POLLIN)
            while (!found && (n = read(fd, buf, sizeof buf - 1)) > 0) buf[n] = 0, found = here(buf, n, nonce);
    }
    close(fd);
    if (found) say("pair: found %s:%s", pair.host, pair.port);
    return found;
}

static void fail(int state, const char *why) {
    pair.state = state;
    copy(pair.why, sizeof pair.why, why);
    say("pair: %s", why);
}

static void denied(const char *reason) {
    static const char *const words[][2] = {
        {"idle", "Start pairing on the server first (soloify pair-device)"},
        {"address", "Pairing works only on the home network"},
        {"rate", "Too many tries: wait a minute"},
        {"malformed", "The server didn't understand the request"},
        {"code", "The code typed on the server was wrong"},
        {"timeout", "No code was typed on the server in time"},
        {"replaced", "Another device asked the server to pair"},
        {"cancelled", "Pairing was cancelled on the server"},
        {"gone", "The connection to the server was lost"},
    };
    for (unsigned i = 0; i < sizeof words / sizeof *words; ++i)
        if (!strcmp(reason, words[i][0])) {
            fail(PAIR_DENIED, words[i][1]);
            return;
        }
    fail(PAIR_DENIED, "The server refused");
}

/* REMOTE's folder with name after it, as remote_setting finds .delay */
static void beside(const char *remote, const char *name, char *out, int cap) {
    int dir = strlen(remote);
    while (dir && remote[dir - 1] != '/') --dir;
    snprintf(out, cap, "%.*s%s", dir, remote, name);
}

static int read_small(const char *file, char *buf, int cap) {
    int fd = open(file, 0), n = 0, r;
    if (fd < 0) return buf[0] = 0, -1;
    while (n < cap - 1 && (r = read(fd, buf + n, cap - 1 - n)) > 0) n += r;
    close(fd);
    buf[n] = 0;
    return n;
}

static int write_file(const char *dest, const char *data, int n) {
    char tmp[300];
    int fd, off = 0, r;
    snprintf(tmp, sizeof tmp, "%s.tmp", dest);
    if ((fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0600)) < 0) return 0;
    while (off < n && (r = write(fd, data + off, n - off)) > 0) off += r;
    int ok = off == n && !fsync(fd);
    close(fd);
    if (ok && !rename(tmp, dest)) return 1;
    unlink(tmp);
    return 0;
}

static const char *count_file(void) {
    const char *s = getenv("SOLOIFY_AWAY_COUNT");
    return s && *s ? s : AWAY_COUNT;
}

/* The host of away's last verified line in file, and its time */
static int verified(const char *file, char *host, int cap, long long *when) {
    char buf[4096], *line, *end;
    int found = 0;
    if (read_small(file, buf, sizeof buf) <= 0) return 0;
    for (line = buf; *line; line = end) {
        end = strchr(line, '\n');
        end = end ? (*end = 0, end + 1) : line + strlen(line);
        if (strncmp(line, "verified ", 9)) continue;
        char *t = line + 9, *h = strchr(t, ' ');
        if (!h || !h[1]) continue;
        *h++ = 0;
        copy(host, cap, h);
        *when = strtol(t, NULL, 10);
        found = 1;
    }
    return found;
}

/* A new pairing: the folder's away goes unless it verified this WAN host, and the failures count
 * starts again. */
static void away_paired(const char *remote, const char *wan) {
    char file[300], host[128];
    long long when;
    beside(remote, "away", file, sizeof file);
    if (verified(file, host, sizeof host, &when) && strcmp(host, wan)) unlink(file);
    else if (!*wan) unlink(file);
    unlink(count_file());
}

static int store(const char *remote, const char *also, const char *text, int n, const char *wan) {
    char other[300];
    int ok;
    away_paired(remote, wan);
    ok = write_file(remote, text, n);
    if (ok && also) {
        snprintf(other, sizeof other, "%s/remote", also);
        away_paired(other, wan);
        ok = write_file(other, text, n);
    }
    return ok;
}

/* The first line is there, and the lines it announces */
static int answered(const char *buf) {
    const char *nl = strchr(buf, '\n');
    int lines = 0;
    if (!nl) return 0;
    if (strncmp(buf, "remote 1 ", 9)) return 1;
    for (const char *p = nl + 1; (p = strchr(p, '\n')); ++p) ++lines;
    return lines >= strtol(buf + 9, NULL, 10);
}

/* remote 1 N, then N lines, each one remote_parse reads with the pin F */
static void take(const char *remote, const char *also, char *buf, const unsigned char f[32]) {
    struct endpoint ep;
    char *line = strchr(buf, '\n') + 1, *end, one[512], wan[128] = "";
    long n = strtol(buf + 9, &end, 10);
    if (*end != '\n' || n < 1 || n > MAX_ENDPOINTS) {
        fail(PAIR_FAILED, "The server's answer was unreadable");
        return;
    }
    char *body = line;
    for (long i = 0; i < n; ++i, line = end + 1) {
        end = strchr(line, '\n');
        if (!end || end - line >= (int)sizeof one) {
            fail(PAIR_FAILED, "The server's answer was unreadable");
            return;
        }
        memcpy(one, line, end - line);
        one[end - line] = 0;
        if (remote_parse(one, &ep) < 0 || !ep.pinned || memcmp(ep.pin, f, 32)) {
            fail(PAIR_FAILED, "The server's answer didn't match its certificate");
            return;
        }
        if (i == 1) copy(wan, sizeof wan, ep.host);
    }
    if (!store(remote, also, body, line - body, wan)) {
        fail(PAIR_FAILED, "Couldn't save the pairing on the card");
        return;
    }
    pair.state = PAIR_PAIRED;
    say("pair: paired, %ld endpoint%s", n, n > 1 ? "s" : "");
}

static void exchange(const char *remote, const char *also, const unsigned char nonce[16], const char *nhex) {
    struct endpoint ep;
    struct link l;
    char buf[ANSWER_MAX], why[160];
    int n = 0, len, eof = 0;
    uint64_t deadline;
    memset(&ep, 0, sizeof ep);
    copy(ep.host, sizeof ep.host, pair.host);
    snprintf(ep.port, sizeof ep.port, "%s", pair.port);
    ep.unchecked = ep.lan = 1;
    pair.state = PAIR_CONNECTING;
    if (link_open(&l, &ep) < 0) {
        if (stopped()) return;
        snprintf(why, sizeof why, "Couldn't connect to %s", pair.host);
        fail(PAIR_FAILED, why);
        return;
    }
    pair_code(l.peer, nonce, pair.code);
    len = snprintf(buf, sizeof buf, "pair 1 %s %s\n", nhex, name());
    deadline = now_ms() + 5000;
    for (int off = 0; off < len && !eof;) {
        int w = link_write(&l, buf + off, len - off);
        if (w < 0) eof = 1;
        else if (w) off += w;
        else if (!wait_for(l.fd, POLLOUT, deadline)) eof = 1;
    }
    if (!eof) {
        pair.state = PAIR_CODE;
        say("pair: enter %s on the Soloify server", pair.code);
        for (deadline = now_ms() + ANSWER_MS; n < (int)sizeof buf - 1;) {
            int r = link_read(&l, buf + n, sizeof buf - 1 - n);
            if (r < 0) {
                eof = 1;
                break;
            }
            buf[n += r] = 0;
            if (answered(buf)) break;
            if (!r && !wait_for(l.fd, POLLIN, deadline)) break;
        }
    }
    buf[n] = 0;
    link_close(&l);
    if (pair.cancel || quitting) return;
    if (!answered(buf)) fail(PAIR_FAILED, !eof ? "No answer from the server"
                                          : n ? "The server's answer was unreadable"
                                              : "The server's Soloify can't pair: update it");
    else if (!strncmp(buf, "denied\n", 7) || !strncmp(buf, "denied ", 7)) {
        char *nl = strchr(buf, '\n');
        *nl = 0;
        denied(buf[6] ? buf + 7 : "");
    } else if (!strncmp(buf, "remote 1 ", 9)) take(remote, also, buf, l.peer);
    else fail(PAIR_FAILED, "The server's answer was unreadable");
}

void pair_run(const char *remote, const char *also, const char *host, const char *port) {
    unsigned char nonce[16];
    char nhex[33];
    pair.cancel = 0;
    pair.code[0] = pair.why[0] = 0;
    if (RAND_bytes(nonce, sizeof nonce) != 1) {
        fail(PAIR_FAILED, "No random numbers for pairing"), ++pair.outcomes;
        return;
    }
    hex(nonce, 16, nhex);
    if (host) {
        copy(pair.host, sizeof pair.host, host);
        snprintf(pair.port, sizeof pair.port, "%s", port && *port ? port : "7443");
    } else {
        pair.state = PAIR_LOOKING;
        if (!discover(nhex)) {
            pair.state = stopped() ? PAIR_IDLE : PAIR_NOT_FOUND;
            if (pair.state == PAIR_NOT_FOUND) say("pair: no bridge answered"), ++pair.outcomes;
            return;
        }
    }
    exchange(remote, also, nonce, nhex);
    if (pair.cancel || quitting) pair.state = PAIR_IDLE;
    else ++pair.outcomes;
}

int pair_waiting(void) {
    return pair.state == PAIR_LOOKING || pair.state == PAIR_CONNECTING || pair.state == PAIR_CODE;
}

void pair_message(char *out, int cap) {
    switch (pair.state) {
    case PAIR_CODE: snprintf(out, cap, "Pair: enter %s on your Soloify server", pair.code); break;
    case PAIR_CONNECTING:
        snprintf(out, cap, "Pair: connecting to your Soloify server. Retry in a moment for the code");
        break;
    case PAIR_NOT_FOUND:
        snprintf(out, cap, "Pair: no Soloify server answered. Run soloify pair-device on it, then Retry");
        break;
    case PAIR_PAIRED: snprintf(out, cap, "Paired. Retry to connect"); break;
    case PAIR_DENIED:
    case PAIR_FAILED: snprintf(out, cap, "Pair: %s. Retry to pair again", pair.why); break;
    default: snprintf(out, cap, "Pair: looking for your Soloify server. Retry in a moment for the code");
    }
}

/* Away */

static int count_get(void) {
    char buf[16];
    return read_small(count_file(), buf, sizeof buf) > 0 ? strtol(buf, NULL, 10) : 0;
}

static void count_set(int v) {
    char text[16];
    if (!v) {
        if (count_get()) unlink(count_file());
        return;
    }
    write_file(count_file(), text, snprintf(text, sizeof text, "%d\n", v));
}

static long long unix_time(void) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return t.tv_sec;
}

void away_round(const char *remote, const struct endpoint *eps, int n, int up) {
    char file[300], host[128], line[180];
    long long when;
    if (up < 0) {
        if (n >= 2) count_set(count_get() + 1); /* the WAN line was tried, and failed */
        return;
    }
    count_set(0);
    if (!up) return;
    beside(remote, "away", file, sizeof file);
    if (verified(file, host, sizeof host, &when) && !strcmp(host, eps[up].host)) return;
    int fd = open(file, O_WRONLY | O_CREAT | O_APPEND, 0644), len;
    if (fd < 0) return;
    len = snprintf(line, sizeof line, "verified %lld %s\n", unix_time(), eps[up].host);
    if (write(fd, line, len) == len) say("away: %s verified", eps[up].host);
    close(fd);
}

int away_state(const char *remote, const char *const *dirs, int ndirs, long long *when) {
    struct endpoint eps[MAX_ENDPOINTS];
    char file[300], host[128];
    long long at;
    int state = AWAY_UNVERIFIED;
    *when = 0;
    if (remote_load(remote, eps, MAX_ENDPOINTS) < 2) return AWAY_NONE;
    if (count_get() >= UNREACHED) return AWAY_UNREACHED;
    for (int i = 0; i < ndirs; ++i) {
        snprintf(file, sizeof file, "%s/away", dirs[i]);
        if (verified(file, host, sizeof host, &at) && !strcmp(host, eps[1].host) && at >= *when)
            state = AWAY_VERIFIED, *when = at;
    }
    return state;
}
