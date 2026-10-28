/* librespot's device contract. Firmware takes the DAC on playing, and treats any other state
 * as proof that the sink has released it. Metadata waits on the audio packet clock, including
 * through a pause/refill. SOLOIFY_Q2POD isolates the paths for host and QEMU tests. */
#include "relay.h"
#define TEXT 1536
#define STATES 64
#define COVERS 4
#define CLIENTS 4
static int enabled, control = -1, browser = -1, lockfd = -1, online, configured;
static char base[96], current[TEXT] = "state=none\n", upstream[TEXT] = "state=none\n";
static char prime[TEXT]; /* position of the first audio after connect or a reset */
static uint64_t stamped;
static int held, commands, resetting;
static struct { char text[TEXT]; uint64_t sample; } states[STATES];
static unsigned first, count;
static struct { char id[64]; unsigned char *data; int size; } covers[COVERS];
static unsigned next_cover;
static struct { int fd, n, sent, out, bad; uint64_t end; char buf[8193], reply[512]; } clients[CLIENTS];

static void value(const char *text, const char *key, char *out, int cap) {
    int k = strlen(key);
    *out = 0;
    for (const char *p = text; *p;) {
        const char *end = strchr(p, '\n');
        int len = end ? end - p : (int)strlen(p);
        if (len > k && !memcmp(p, key, k) && p[k] == '=') {
            int n = len - k - 1;
            if (n >= cap) n = cap - 1;
            memcpy(out, p + k + 1, n);
            out[n] = 0;
            return;
        }
        p += len + (end != NULL);
    }
}
static uint64_t number(const char *text, const char *key) {
    char buf[32];
    uint64_t v = 0;
    value(text, key, buf, sizeof buf);
    for (char *p = buf; *p >= '0' && *p <= '9'; ++p) v = v * 10 + *p - '0';
    return v;
}
static int is(const char *text, const char *word) {
    char buf[16];
    value(text, "state", buf, sizeof buf);
    return !strcmp(buf, word);
}
static void path(char *out, const char *suffix) { snprintf(out, 128, "%s%s", base, suffix); }
static int put(const char *suffix, const void *data, int n) {
    char dest[128], tmp[144];
    int fd, off = 0, r;
    path(dest, suffix);
    snprintf(tmp, sizeof tmp, "%s.new", dest); /* .tmp is librespot's DIRECTORY */
    if ((fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644)) < 0) return 0;
    while (off < n) {
        r = write(fd, (const char *)data + off, n - off);
        if (r < 0 && relay_errno == EINTR) continue;
        if (r <= 0) break;
        off += r;
    }
    close(fd);
    if (off == n && !rename(tmp, dest)) return 1;
    unlink(tmp);
    return 0;
}
/* Rebuild a bounded state, excluding cover and at from the bridge. */
static void publish(const char *text, const char *word, uint64_t position) {
    static const char *keys[] = {"track", "title", "artist", "album", "duration"};
    char out[TEXT], v[256], id[64];
    int n = snprintf(out, sizeof out, "state=%s\n", word);
    if (strcmp(word, "playing")) audio_enable(0); /* kill and REAP before rename */
    if (strcmp(word, "none")) {
        for (unsigned i = 0; i < sizeof keys / sizeof *keys; ++i) {
            value(text, keys[i], v, sizeof v);
            n += snprintf(out + n, sizeof out - n, "%s=%s\n", keys[i], v);
        }
        n += snprintf(out + n, sizeof out - n, "position=%llu\n", (unsigned long long)position);
        value(text, "track", id, sizeof id);
        for (int i = 0; *id && i < COVERS; ++i)
            if (covers[i].data && !strcmp(covers[i].id, id)) {
                if (put(".state.jpg", covers[i].data, covers[i].size))
                    n += snprintf(out + n, sizeof out - n, "cover=1\n");
                break;
            }
    }
    memcpy(current, out, n + 1);
    stamped = now_ms();
    n += snprintf(out + n, sizeof out - n, "at=%llu\n", (unsigned long long)stamped);
    put(".state", out, n);
    if (!strcmp(word, "playing")) audio_enable(1);
}
static uint64_t position(void) {
    uint64_t p = number(current, "position"), duration = number(current, "duration");
    if (is(current, "playing")) p += now_ms() - stamped;
    return duration && p > duration ? duration : p;
}
void q2pod_started(void) {
    if (enabled && *prime && !held && !resetting) {
        publish(prime, "playing", number(prime, "position"));
        *prime = 0;
    }
}
static int bind_socket(const char *suffix, int type) {
    struct sockaddr_un addr;
    int fd = socket(AF_UNIX, type, 0);
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s%s", base, suffix);
    unlink(addr.sun_path);
    if (fd < 0 || bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        if (fd >= 0) close(fd);
        return -1;
    }
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    return fd;
}
void q2pod_start(void) {
    const char *prefix = getenv("SOLOIFY_Q2POD");
    char file[128];
    if (prefix && strlen(prefix) > 94) {
        say("q2pod: socket prefix too long");
        quitting = 1;
        return;
    }
    snprintf(base, sizeof base, "%s", prefix && *prefix ? prefix : "/tmp/q2-librespot");
    path(file, ".lock");
    lockfd = open(file, O_WRONLY | O_CREAT, 0600);
    if (lockfd < 0 || flock(lockfd, LOCK_EX | LOCK_NB) < 0) {
        say("q2pod: another relay owns %s", base);
        quitting = 1;
        return;
    }
    fcntl(lockfd, F_SETFD, FD_CLOEXEC);
    enabled = 1;
    for (int i = 0; i < CLIENTS; ++i) clients[i].fd = -1;
    control = bind_socket(".sock", SOCK_DGRAM);
    browser = bind_socket(".browse.sock", SOCK_STREAM);
    if (control < 0 || browser < 0 || listen(browser, CLIENTS) < 0) {
        say("q2pod: can't bind the local sockets");
        quitting = 1;
    }
    publish("", "none", 0);
    say("q2pod: state and controls ready at %s", base);
}
void q2pod_config(int valid) { configured = valid; }
void q2pod_link(int up) {
    online = up;
    commands = resetting = 0;
    if (enabled && up && held) {
        if (!relay_send('c', "s", 1)) ++commands;
        relay_send('A', "0 pause", 7);
    }
}
int q2pod_wants(void) { return !enabled || (!held && !resetting && is(upstream, "playing")); }
int q2pod_audio(void) { return !enabled || !resetting; }
static void command(char *buf, int n) {
    int reset = buf[0] == 'n' || buf[0] == 'b' || buf[0] == 'S';
    if (buf[0] == 'p') buf[0] = held || !is(current, "playing") ? 'P' : 's';
    if (buf[0] == 's') {
        held = 1;
        publish(current, "paused", position());
    } else if (buf[0] == 'P') {
        held = 0;
        publish(current, "playing", position());
    } else if (reset) {
        count = first = 0;
        *prime = 0;
        audio_clear();
    } else return;
    if (!relay_send('c', buf, n)) {
        if (reset) ++resetting;
        else ++commands;
    }
    if (buf[0] == 's') relay_send('A', "0 pause", 7);
}
void q2pod_frame(int type, const unsigned char *p, int n) {
    if (!enabled) return;
    if (type == 'x') {
        if (n && (p[0] == 's' || p[0] == 'P')) {
            if (commands) --commands;
        } else {
            if (resetting) --resetting;
            count = first = 0;
            *prime = 0;
            audio_clear();
        }
    } else if (type == 's' && n > 0 && n < TEXT && !memchr(p, 0, n)) {
        char text[TEXT], word[16];
        memcpy(text, p, n);
        text[n] = 0;
        value(text, "state", word, sizeof word);
        if (strcmp(word, "none") && strcmp(word, "stopped") && strcmp(word, "paused") && strcmp(word, "playing")) return;
        memcpy(upstream, text, n + 1);
        if (resetting) return;
        if (commands && ((!strcmp(word, "playing")) == held)) return;
        if (strcmp(word, "playing")) {
            if (commands) return;
            relay_send('A', "0 pause", 7);
            if (!strcmp(word, "paused")) {
                char before[64], after[64];
                value(current, "track", before, sizeof before);
                value(text, "track", after, sizeof after);
                held = 1;
                if (is(current, "playing")) publish(current, "paused", position());
                else if (!count && (!is(current, "paused") || (!audio_received() && strcmp(before, after))))
                    publish(text, word, number(text, "position"));
            } else {
                held = 0;
                count = first = 0;
                *prime = 0;
                audio_clear();
                publish(text, word, number(text, "position"));
            }
        } else {
            if (!commands) held = 0;
            if (count == STATES) first = (first + 1) % STATES, --count;
            unsigned at = (first + count++) % STATES;
            memcpy(states[at].text, text, n + 1);
            states[at].sample = audio_received();
            if (!audio_received()) memcpy(prime, text, n + 1);
            if (!held && !is(current, "playing")) publish(current, "playing", position());
        }
    } else if (type == 'j') {
        const unsigned char *nl = memchr(p, '\n', n);
        if (!nl || nl == p || nl - p >= 64 || n - (nl + 1 - p) < 4 || memchr(p, 0, nl - p)) return;
        int size = n - (nl + 1 - p);
        if (nl[1] != 0xff || nl[2] != 0xd8) return;
        unsigned at = next_cover++ % COVERS;
        free(covers[at].data);
        covers[at].data = malloc(size);
        if (!covers[at].data) return;
        memcpy(covers[at].id, p, nl - p);
        covers[at].id[nl - p] = 0;
        memcpy(covers[at].data, nl + 1, size);
        covers[at].size = size;
        char id[64], word[16], has_cover[8];
        value(current, "track", id, sizeof id);
        value(current, "state", word, sizeof word);
        value(current, "cover", has_cover, sizeof has_cover);
        if (!strcmp(id, covers[at].id) && strcmp(has_cover, "1") && !resetting)
            publish(current, word, position()); /* a late cover also appears while paused */
    }
}
static void reply(int i) {
    char *buf = clients[i].buf, *line, *id, *op, *end;
    const char *status = "bad", *message = "Malformed request";
    id = "0";
    if (clients[i].bad) buf[0] = 0;
    if (!strncmp(buf, "Q2SPOT/1\n", 9) && (line = strchr(buf + 9, '\n')) && !line[1]) {
        *line = 0;
        id = buf + 9;
        end = strchr(id, '\t');
        if (end) {
            *end = 0;
            op = end + 1;
            int valid = *id != 0 && strlen(id) <= 10;
            for (char *c = id; *c; ++c) if (*c < '0' || *c > '9') valid = 0;
            if (valid) {
                retry_remote = 1;
                if (!configured) status = "error", message = "Copy remote to .soloify (see the Soloify guide)";
                else if (!online) status = "offline", message = "Can't reach the Spotify bridge";
                else if (!strcmp(op, "caps")) status = "ok", message = "soloify step 3";
                else if (!strcmp(op, "account")) {
                    if (is(upstream, "none")) status = "error", message = "Pair Soloify on its server and choose Q2 in Spotify";
                    else status = "ok", message = "";
                } else status = "unsupported", message = "Browsing comes in Soloify step 4";
            } else id = "0";
        } else id = "0";
    }
    clients[i].out = snprintf(clients[i].reply, sizeof clients[i].reply, "Q2SPOT/1\n%s\t%s\t%s\t\n", id, status, message);
}
static void browse(void) {
    for (int i = 0; i < CLIENTS; ++i) {
        int r;
        if (clients[i].fd < 0) {
            if ((r = accept(browser, NULL, NULL)) < 0) continue;
            clients[i].fd = r;
            clients[i].n = clients[i].out = clients[i].sent = clients[i].bad = 0;
            clients[i].buf[0] = 0;
            clients[i].end = now_ms() + 2000;
            fcntl(r, F_SETFD, FD_CLOEXEC);
            fcntl(r, F_SETFL, fcntl(r, F_GETFL) | O_NONBLOCK);
        }
        if (!clients[i].out) {
            int n = clients[i].n;
            char discard[1024];
            r = read(clients[i].fd, n == 8192 ? discard : clients[i].buf + n,
                     n == 8192 ? sizeof discard : sizeof clients[i].buf - 1 - n);
            if (r > 0) {
                if (n == 8192) clients[i].bad = 1;
                else clients[i].n += r;
                clients[i].buf[clients[i].n] = 0;
                if (memchr(clients[i].buf, 0, clients[i].n)) clients[i].bad = 1;
            } else if (!r) reply(i);
            else if (relay_errno != EAGAIN && relay_errno != EINTR) clients[i].end = 0;
        }
        if (clients[i].out) {
            r = write(clients[i].fd, clients[i].reply + clients[i].sent, clients[i].out - clients[i].sent);
            if (r > 0) clients[i].sent += r;
            else if (r < 0 && relay_errno != EAGAIN && relay_errno != EINTR) clients[i].end = 0;
        }
        if ((clients[i].out && clients[i].sent == clients[i].out) || now_ms() >= clients[i].end) {
            close(clients[i].fd);
            clients[i].fd = -1;
        }
    }
}
void q2pod_tick(void) {
    char buf[64];
    int n;
    if (!enabled) return;
    while ((n = read(control, buf, sizeof buf - 1)) > 0) {
        buf[n] = 0;
        int valid = n == 1 && strchr("pPsnb", buf[0]) != NULL;
        if (buf[0] == 'S') {
            char *p = buf + 1;
            while (*p == ' ') ++p;
            valid = *p >= '0' && *p <= '9';
            for (; *p; ++p) if (*p < '0' || *p > '9') valid = 0;
        }
        if (valid && n < 63) command(buf, n);
    }
    while (count && !held && !resetting && audio_played() >= states[first].sample) {
        char *text = states[first].text;
        uint64_t pos = number(text, "position");
        if (*prime) pos = pos > (unsigned)audio_target() ? pos - audio_target() : 0;
        publish(text, "playing", pos);
        first = (first + 1) % STATES;
        --count;
    }
    browse();
}
void q2pod_stop(void) {
    if (!enabled) return;
    audio_enable(0);
    const char *suffixes[] = {".sock", ".browse.sock", ".state", ".state.jpg"};
    char file[128];
    for (unsigned i = 0; i < sizeof suffixes / sizeof *suffixes; ++i) {
        path(file, suffixes[i]);
        unlink(file);
    }
    for (int i = 0; i < CLIENTS; ++i) if (clients[i].fd >= 0) close(clients[i].fd);
    close(control), close(browser), close(lockfd);
    for (int i = 0; i < COVERS; ++i) free(covers[i].data);
    enabled = 0;
}
