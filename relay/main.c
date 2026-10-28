/* q2-soloify [-v] [-q] [-b MS] [-r REMOTE]: the card's relay. It dials the endpoints in REMOTE
 * (./remote) in order, says hello, asks for audio and plays it; on a dead link it reconnects, with
 * a backoff of 1 to 15 s while every endpoint fails. -b overrides the buffer target, otherwise
 * the endpoint's buffer=, else 1 s on the LAN and 4 s beyond it. -q enables the device contract. */
#include "relay.h"

#define HELLO_MS 5000
#define PROBE_MS 1000 /* A 1 again, while audio is wanted */
#define ACK_MS 200
#define ACK_IDLE_MS 5000 /* the bridge drops a relay silent for 20 s */
#define STATS_MS 10000
#define DEAD_MS 6000 /* no bytes while audio is wanted: the link is gone */
#define BACKOFF_MAX_MS 15000

int verbose;
volatile int quitting, retry_remote;
static uint64_t started;
static struct link conn = {.fd = -1};
static struct wire wire;
static unsigned reconnects;
static int connected;

int relay_send(char type, const void *payload, int n) {
    if (!connected) return -1;
    if (wire_send(&wire, type, payload, n) < 0) {
        /* Never silently lose a pause/seek: recover a full output queue as a broken link. */
        connected = 0;
        return -1;
    }
    return 0;
}

static void stop(int sig) { quitting = 1; }

uint64_t now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

void say(const char *fmt, ...) {
    char buf[512];
    unsigned ms = now_ms() - started;
    int n = snprintf(buf, sizeof buf, "[%6u.%u] ", ms / 1000, ms % 1000 / 100);
    va_list ap;
    va_start(ap, fmt);
    n += vsnprintf(buf + n, sizeof buf - n - 1, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof buf - 2) n = sizeof buf - 2;
    buf[n++] = '\n';
    write(2, buf, n);
}

int relay_wait(int fd, short events, uint64_t deadline) {
    for (;;) {
        if (quitting || (fd < 0 && retry_remote)) return 0;
        struct pollfd p[2];
        int n = 0, sink, r;
        uint64_t now = now_ms();
        if (fd >= 0) p[n].fd = fd, p[n].events = events, p[n].revents = 0, ++n;
        sink = audio_pollfd(&p[n]) ? n++ : -1;
        /* At least a packet's pace, for the sink's real-time feed. A deadline already past still
         * polls once, so a late caller never starves the link or the sink. */
        r = poll(p, n, now >= deadline ? 0 : deadline - now > FRAME_MS ? FRAME_MS : (int)(deadline - now));
        if (r < 0 && relay_errno != EINTR) return 0;
        if (sink >= 0 && p[sink].revents) audio_service(p[sink].revents);
        audio_tick();
        q2pod_tick();
        if (fd >= 0 && p[0].revents) return p[0].revents;
        if (now >= deadline) return 0;
    }
}

static int flush(void) {
    while (wire.nout) {
        int w = link_write(&conn, wire.out, wire.nout);
        if (w < 0) return -1;
        if (!w) break;
        memmove(wire.out, wire.out + w, wire.nout - w);
        wire.nout -= w;
    }
    return 0;
}

/* Reads what the link has; -1 once it's gone */
static int pull(uint64_t *last_rx) {
    for (;;) {
        int r = link_read(&conn, wire.in + wire.nin, sizeof wire.in - wire.nin);
        if (r < 0) return -1;
        if (!r) return 0;
        wire.nin += r;
        *last_rx = now_ms();
        if (wire.nin == sizeof wire.in) return 0;
    }
}

static int hello(const struct endpoint *ep) {
    char line[128];
    int n = snprintf(line, sizeof line, "hello 1 %s\n", ep->token);
    uint64_t deadline = now_ms() + HELLO_MS, rx = 0;
    memset(&wire, 0, sizeof wire);
    memcpy(wire.out, line, n);
    wire.nout = n;
    while (wire.nout)
        if (flush() < 0 || (wire.nout && !relay_wait(conn.fd, POLLOUT, deadline))) return -1;
    for (;;) {
        unsigned char *nl;
        if (pull(&rx) < 0) return -1;
        if ((nl = memchr(wire.in, '\n', wire.nin))) {
            n = nl + 1 - wire.in;
            if (n != 5 || memcmp(wire.in, "ok 1\n", 5)) return -1;
            memmove(wire.in, wire.in + n, wire.nin - n);
            wire.nin -= n;
            return 0;
        }
        if (!relay_wait(conn.fd, POLLIN, deadline)) return -1;
    }
}

static void stats(const struct endpoint *ep, unsigned received) {
    char buf[384];
    int n = audio_stats(buf, sizeof buf);
    n += snprintf(buf + n, sizeof buf - n, " rx=%u reconnects=%u ep=%s", received, reconnects, ep->host);
    wire_send(&wire, 't', buf, n);
    if (verbose) say("%s", buf);
}

/* One connection, until its link dies */
static void session(const struct endpoint *ep, int target) {
    unsigned received = 0, acked = 0;
    uint64_t now = now_ms(), last_rx = now, probe = now, ack = now + ACK_MS, last_ack = now;
    const char *every = getenv("SOLOIFY_STATS_MS"); /* for tests */
    int stats_ms = every ? strtol(every, NULL, 10) : STATS_MS;
    uint64_t next_stats = now + stats_ms;
    say("connected to %s:%s, buffer %d ms", ep->host, ep->port, target);
    audio_start(target);
    connected = 1;
    q2pod_link(1);
    for (; !quitting && connected;) {
        const unsigned char *payload;
        int type, n, ev;
        uint64_t wake;
        now = now_ms();
        if (now >= probe) {
            char want[16];
            if (q2pod_wants()) wire_send(&wire, 'A', want, snprintf(want, sizeof want, "1 %d", target));
            probe = now + PROBE_MS;
        }
        /* The timer moves on even with nothing to ack: left in the past, it would wake the loop at
         * once, every time, while the bridge sends nothing */
        if (now >= ack) {
            if (received != acked || now >= last_ack + ACK_IDLE_MS) {
                unsigned char b[4];
                put_be32(b, received);
                if (!wire_send(&wire, 'k', b, 4)) acked = received, last_ack = now;
            }
            ack = now + ACK_MS;
        }
        if (now >= next_stats) stats(ep, received), next_stats = now + stats_ms;
        if (q2pod_wants() && now >= last_rx + DEAD_MS) {
            say("nothing from the bridge for %d s", DEAD_MS / 1000);
            break;
        }
        if (flush() < 0) break;
        wake = probe < ack ? probe : ack;
        if (next_stats < wake) wake = next_stats;
        ev = relay_wait(conn.fd, POLLIN | (wire.nout ? POLLOUT : 0), wake);
        if ((ev & (POLLIN | POLLERR | POLLHUP)) && pull(&last_rx) < 0) {
            say("the bridge closed the link");
            break;
        }
        while ((type = wire_next(&wire, &payload, &n)) > 0) {
            if (type == 'a' && n > 4) {
                if (q2pod_audio()) audio_packet(payload + 4, n - 4);
                ++received; /* ack discarded audio too, so a reset never blocks flow control */
            } else q2pod_frame(type, payload, n);
            wire_consume(&wire);
        }
        if (type < 0) {
            say("a frame too big for the relay");
            break;
        }
    }
    connected = 0;
    q2pod_link(0);
    link_close(&conn);
    ++reconnects;
}

int main(int argc, char **argv) {
    const char *path = "remote";
    struct endpoint eps[MAX_ENDPOINTS];
    int target_opt = 0, backoff = 1000, q2pod = 0;
    started = now_ms();
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "-v")) verbose = 1;
        else if (!strcmp(argv[i], "-q")) q2pod = 1;
        else if (!strcmp(argv[i], "-b") && i + 1 < argc) target_opt = strtol(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "-r") && i + 1 < argc) path = argv[++i];
        else {
            say("usage: q2-soloify [-v] [-q] [-b MS] [-r REMOTE]");
            return 2;
        }
    }
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    const char *parent = getenv("SOLOIFY_PARENT");
    if (parent) {
        prctl(PR_SET_PDEATHSIG, SIGTERM, 0, 0, 0);
        if (getppid() != strtol(parent, NULL, 10)) quitting = 1;
    }
    if (target_opt && (target_opt < 200 || target_opt > 20000)) return 2;
    if (q2pod) q2pod_start();
    for (; !quitting;) {
        int n = remote_load(path, eps, MAX_ENDPOINTS), up = 0;
        retry_remote = 0;
        q2pod_config(n > 0);
        if (n <= 0) say("no endpoint in %s: copy remote to .soloify (see the Soloify guide)", path);
        for (int i = 0; i < n && !up && !quitting; ++i) {
            if (link_open(&conn, &eps[i]) < 0) continue;
            if (hello(&eps[i]) < 0) {
                say("%s: the bridge refused the hello", eps[i].host);
                link_close(&conn);
                continue;
            }
            up = 1;
            session(&eps[i], target_opt ? target_opt : eps[i].buffer_ms ? eps[i].buffer_ms : eps[i].lan ? 1000 : 4000);
        }
        if (up) {
            backoff = 1000;
            continue;
        }
        relay_wait(-1, 0, now_ms() + backoff);
        backoff = backoff * 2 > BACKOFF_MAX_MS ? BACKOFF_MAX_MS : backoff * 2;
    }
    audio_clear();
    q2pod_stop();
    return 0;
}
