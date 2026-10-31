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
static int held, commands, resetting, fresh_wait, restore_ok = 1, pause_active;
static unsigned pause_id;
static char bookmark[TEXT];
#define ANCHORS 2048 /* More than the largest ring, plus the hardware/decoder lead. */
static struct { char text[TEXT]; uint64_t sample; } anchors[ANCHORS];
static unsigned anchor_first, anchor_count;
static void command(char *buf, int n);
static struct { char text[TEXT]; uint64_t sample; } states[STATES];
static unsigned first, count;
static struct { char id[64]; unsigned char *data; int size; } covers[COVERS];
static unsigned next_cover;
static struct { int fd, n, sent, out, bad, waiting; unsigned correlation;
    uint64_t end; char id[11], buf[8193], reply[65537]; } clients[CLIENTS];
static unsigned next_request;
static void local_reply(int i, const char *status, const char *message) {
    clients[i].waiting = 0;
    clients[i].out = snprintf(clients[i].reply, sizeof clients[i].reply,
        "Q2SPOT/1\n%s\t%s\t%s\t\n", *clients[i].id ? clients[i].id : "0", status, message);
}

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
static void clear_pause_error(void) {
    char file[128];
    path(file, ".pause-error");
    unlink(file);
}
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
/* Snapshot before killing the sink: audio_clear deliberately forgets its clocks. */
void q2pod_paused(void) {
    if (!enabled) return;
    if (pause_active) { /* Repeated or rapid Pause retains the original bookmark. */
        if (held) return;
        held = 1;
        restore_ok = 0;
        fresh_wait = 1;
        audio_enable(0);
        if (!is(current, "paused"))
            publish(current, "paused", *bookmark ? number(bookmark, "position") : position());
        unsigned char request[TEXT + 4];
        put_be32(request, pause_id);
        int n = strlen(bookmark);
        memcpy(request + 4, bookmark, n);
        relay_send('R', request, n + 4); /* order a final source Pause after queued Play */
        return;
    }
    pause_active = 1;
    uint64_t sample = 0, pos = position();
    int valid = audio_bookmark(&sample);
    *bookmark = 0;
    for (unsigned i = 0; valid && i < anchor_count; ++i) {
        unsigned at = (anchor_first + i) % ANCHORS;
        if (anchors[at].sample > sample) break;
        memcpy(bookmark, anchors[at].text, strlen(anchors[at].text) + 1);
        pos = number(bookmark, "position") + sample - anchors[at].sample;
    }
    if (valid && !*bookmark && !audio_received() && *prime && number(prime, "token")) {
        memcpy(bookmark, prime, strlen(prime) + 1);
        pos = number(prime, "position");
    }
    if (*bookmark && !number(bookmark, "token")) {
        *bookmark = 0;
        pos = position();
    }
    say("bookmark clock valid=%d reason=%s capture=%llu sample=%llu token=%llu anchors=%u",
        valid, valid ? (*bookmark ? "valid" : "missing timeline anchor") : audio_clock_reason(),
        (unsigned long long)audio_received(), (unsigned long long)sample,
        (unsigned long long)number(bookmark, "token"), anchor_count);
    char frozen[TEXT];
    snprintf(frozen, sizeof frozen, "%s", *bookmark ? bookmark : current);
    if (*bookmark) {
        /* Send only the identity and exact position. The bridge owns immutable track/context
         * records, so the device never asks it to play an arbitrary supplied URI. */
        snprintf(bookmark, sizeof bookmark, "token=%llu\nposition=%llu\n",
                 (unsigned long long)number(frozen, "token"), (unsigned long long)pos);
    }
    held = 1;
    restore_ok = 0;
    fresh_wait = 1;
    ++pause_id;
    if (!pause_id) ++pause_id;
    audio_pause();
    count = first = anchor_count = anchor_first = 0;
    *prime = 0;
    publish(frozen, "paused", pos);
    unsigned char request[TEXT + 4];
    put_be32(request, pause_id);
    int n = strlen(bookmark);
    memcpy(request + 4, bookmark, n);
    relay_send('R', request, n + 4);
    say("pause bookmark id=%u valid=%d position=%llu", pause_id, n != 0,
        (unsigned long long)pos);
}
void q2pod_capture(void) {
    if (q2pod_ready()) fresh_wait = 0;
}
void q2pod_started(void) {
    if (enabled && !held && !resetting) pause_active = 0;
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

    if (!up) for (int i = 0; i < CLIENTS; ++i)
        if (clients[i].fd >= 0 && clients[i].waiting)
            local_reply(i, "offline", "Bridge disconnected; Retry to reconnect");
    if (enabled && up && held) {
        unsigned char request[TEXT + 4];
        put_be32(request, pause_id);
        int n = strlen(bookmark);
        memcpy(request + 4, bookmark, n);
        relay_send('R', request, n + 4);
        relay_send('A', "0 pause", 7);
    }
}
int q2pod_wants(void) { return !enabled || (!held && restore_ok && !resetting && is(upstream, "playing")); }
int q2pod_audio(void) { return !enabled || (!resetting && restore_ok && !fresh_wait && !held); }
int q2pod_ready(void) { return !enabled || (!commands && q2pod_wants()); }
static void command(char *buf, int n) {
    int reset = buf[0] == 'n' || buf[0] == 'b' || buf[0] == 'S';
    if (buf[0] == 'p')
        buf[0] = held || (!commands && !restore_ok) ||
                 (!pause_active && !is(current, "playing")) ? 'P' : 's';
    if (buf[0] == 's') {
        q2pod_paused();
        return;
    } else if (buf[0] == 'P') {
        held = 0;
        fresh_wait = 1;
        audio_clear();
        count = first = anchor_count = anchor_first = 0;
        *prime = 0;
    } else if (reset) {
        restore_ok = 1;
        clear_pause_error();
        pause_id = pause_active = 0;
        *bookmark = 0;
        held = 0;
        count = first = anchor_count = anchor_first = 0;
        *prime = 0;
        audio_clear();
    } else return;
    if (!relay_send('c', buf, n)) {
        if (reset) ++resetting;
        else ++commands;
    }
}
void q2pod_frame(int type, const unsigned char *p, int n) {
    if (!enabled) return;
    if (type == 'v' && n == 0) {
        q2pod_paused();
    } else if (type == 'v' && n == 1 && p[0] == 'P') {
        /* The phone's Play goes through the same local fresh-capture gate. */
        char play = 'P';
        command(&play, 1);
    } else if (type == 'h' && n >= 5) {
        if (!pause_id || be32(p) != pause_id) return;
        restore_ok = p[4] == 1;
        if (!restore_ok) {
            publish(current, "paused", number(current, "position"));
            int len = n - 5 > 512 ? 512 : n - 5;
            put(".pause-error", p + 5, len);
            say("pause restore failed id=%u: %.*s", pause_id, len, p + 5);
        } else {
            clear_pause_error();
            say("pause restore ready id=%u", pause_id);
        }
    } else if (type == 'l' && n > 0 && n < TEXT && !memchr(p, 0, n)) {
        if (!q2pod_audio()) return;
        if (anchor_count == ANCHORS) anchor_first = (anchor_first + 1) % ANCHORS, --anchor_count;
        unsigned at = (anchor_first + anchor_count++) % ANCHORS;
        memcpy(anchors[at].text, p, n);
        anchors[at].text[n] = 0;
        anchors[at].sample = audio_received();
        if (number(anchors[at].text, "token")) count = first = 0;
    } else if (type == 'r' && n > 4 && n <= 65540) {
        unsigned id = be32(p);
        for (int i = 0; i < CLIENTS; ++i) {
            if (clients[i].fd < 0 || !clients[i].waiting || clients[i].correlation != id) continue;
            char header[32];
            int h = snprintf(header, sizeof header, "Q2SPOT/1\n%s\t", clients[i].id);
            if (n - 4 < h || memcmp(p + 4, header, h) || memchr(p + 4, 0, n - 4)) {
                local_reply(i, "error", "Malformed bridge response");
                return;
            }
            memcpy(clients[i].reply, p + 4, n - 4);
            clients[i].out = n - 4;
            clients[i].waiting = 0;
            return;
        }
    } else if (type == 'B' && n == 4) {
        /* The bridge has finished read-only preparation. A client may already have
         * timed out: the mutation still needs its boundary, independently of its slot. */
        say("reset begin browse=%u", be32(p));
        held = fresh_wait = 0;
        restore_ok = 1;
        clear_pause_error();
        pause_id = pause_active = 0;
        *bookmark = 0;
        anchor_count = anchor_first = 0;
        count = first = 0;
        *prime = 0;
        audio_clear();
        ++resetting;
    } else if (type == 'x') {
        say("boundary op=%c pending=%d commands=%d", n ? p[0] : '?', resetting, commands);
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
        /* Loss of the source is an observation, not a replacement of the user's
         * audible bookmark. Keep its identity and frozen metadata for retry. */
        if (pause_active && (!strcmp(word, "none") || !strcmp(word, "stopped"))) return;
        if (commands && ((!strcmp(word, "playing")) == held)) return;
        if (strcmp(word, "playing")) {
            if (commands) return;
            if (!strcmp(word, "paused")) {
                if (!held && !pause_active && (is(current, "playing") || audio_received()))
                    q2pod_paused();
                if (!held && restore_ok && !commands) {
                    held = 1;
                    publish(text, word, number(text, "position"));
                }
            } else {
                held = fresh_wait = 0;
                restore_ok = 1;
                pause_id = pause_active = 0;
                *bookmark = 0;
                anchor_count = anchor_first = 0;
                relay_send('A', "0 pause", 7);
                count = first = 0;
                *prime = 0;
                audio_clear();
                publish(text, word, number(text, "position"));
            }
        } else {
            if (held || !restore_ok) return;
            if (anchor_count && number(anchors[anchor_first].text, "token")) return;
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
    char *buf = clients[i].buf;
    int n = clients[i].n, start = 9, end;
    if (clients[i].bad || n < 12 || memcmp(buf, "Q2SPOT/1\n", 9) || buf[n-1] != '\n') goto bad;
    for (int k = 9; k < n-1; ++k) {
        if (buf[k] == '\n' || buf[k] == '\r') goto bad;
        if (buf[k] == '%') {
            if (k+2 >= n-1) goto bad;
            for (int j = 1; j <= 2; ++j)
                if (!((buf[k+j] >= '0' && buf[k+j] <= '9') ||
                      (buf[k+j] >= 'A' && buf[k+j] <= 'F') ||
                      (buf[k+j] >= 'a' && buf[k+j] <= 'f'))) goto bad;
            if (buf[k+1] == '0' && buf[k+2] == '0') goto bad;
            k += 2;
        }
    }
    end = start;
    while (end < n-1 && buf[end] >= '0' && buf[end] <= '9') ++end;
    if (end == start || end-start > 10 || buf[end] != '\t') goto bad;
    memcpy(clients[i].id, buf + start, end-start);
    clients[i].id[end-start] = 0;
    start = ++end;
    while (end < n-1 && buf[end] != '\t') {
        if (buf[end] < 'a' || buf[end] > 'z') goto bad;
        ++end;
    }
    if (end == start) goto bad;
    retry_remote = 1;
    if (!configured) local_reply(i, "error", "Copy remote to .soloify (see the Soloify guide)");
    else if (!online) local_reply(i, "offline", "Can't reach the Spotify bridge");
    else {
        unsigned char payload[8196];
        clients[i].correlation = ++next_request;
        put_be32(payload, clients[i].correlation);
        memcpy(payload + 4, buf, n);
        if (relay_send('q', payload, n+4)) local_reply(i, "offline", "Bridge disconnected");
        else clients[i].waiting = 1;
    }
    return;
bad:
    local_reply(i, "bad", "Malformed request");
}
static void browse(void) {
    for (int i = 0; i < CLIENTS; ++i) {
        int r;
        if (clients[i].fd < 0) {
            if ((r = accept(browser, NULL, NULL)) < 0) continue;
            clients[i].fd = r;
            clients[i].n = clients[i].out = clients[i].sent = clients[i].bad = clients[i].waiting = 0;
            clients[i].id[0] = 0;
            clients[i].buf[0] = 0;
            clients[i].end = now_ms() + 11000;
            fcntl(r, F_SETFD, FD_CLOEXEC);
            fcntl(r, F_SETFL, fcntl(r, F_GETFL) | O_NONBLOCK);
        }
        if (clients[i].waiting && now_ms() >= clients[i].end) {
            local_reply(i, "offline", "Browse timed out; Retry");
            clients[i].end = now_ms() + 500;
        }
        if (!clients[i].out && !clients[i].waiting) {
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
    uint64_t rendered;
    if (anchor_count && !held && !resetting && audio_bookmark(&rendered)) {
        unsigned at = anchor_first;
        int found = 0;
        for (unsigned i = 0; i < anchor_count; ++i) {
            unsigned next = (anchor_first + i) % ANCHORS;
            if (anchors[next].sample > rendered) break;
            at = next;
            found = 1;
        }
        if (found && sink_player()) {
            char old[64], fresh[64];
            value(current, "track", old, sizeof old);
            value(anchors[at].text, "track", fresh, sizeof fresh);
            if (number(anchors[at].text, "token") && strcmp(old, fresh))
                publish(anchors[at].text, "playing", number(anchors[at].text, "position")
                        + rendered - anchors[at].sample);
        }
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
    const char *suffixes[] = {".sock", ".browse.sock", ".state", ".state.jpg", ".pause-error"};
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
