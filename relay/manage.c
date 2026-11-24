/* q2-soloify --manage: the Soloify app's page on upstream Q2 Pod (a card app's Q2SPOT/1 page, served
 * on q2pod.c's browse socket at /tmp/q2-soloify, or SOLOIFY_Q2POD). card/run starts it in .soloify,
 * so ./remote, ./.delay and ./.quality are Soloify's own, and ../.spotify is the Spotify slot.
 *
 * The first level (op "menu") is menu_rows: Status (what the Spotify slot holds, whether a remote
 * is there, whether the bridge answers a hello), then Delay and Quality, each a list of choices.
 * The page ends itself after IDLE_MS without a request, and at the reserved op "stop".
 *
 * It runs on the Q2 only (card/run): the M1 Plus's firmware has its own Soloify row and settings.
 *
 * Paths for tests: SOLOIFY_RELAY_BASE (/tmp/q2-librespot, the
 * Spotify slot's pid file) and SOLOIFY_IDLE_MS. */
#include "relay.h"

#define IDLE_MS 120000    /* a card app may end itself when nothing has used it for a while */
#define PROBE_TTL_MS 30000 /* a bridge check is good this long */
#define SLOT_TTL_MS 3000   /* hashing librespot isn't free on the device: once per page view */
#define SLOT "../.spotify"
#define RELAY_BASE "/tmp/q2-librespot"

enum { SLOT_MISSING, SLOT_LIBRESPOT, SLOT_OURS, SLOT_OURS_OLD };
enum { BRIDGE_UNKNOWN, BRIDGE_LAN, BRIDGE_WAN, BRIDGE_DOWN, BRIDGE_BUSY };

static struct {
    int slot; /* SLOT_*, as of slot_at */
    uint64_t slot_at;
    char version[24];
} st;
static struct {
    int state, wanted, running; /* BRIDGE_*; a check asked for; one under way */
    uint64_t at;                /* when state was found */
} probe;
static int stopping;

/* A reply under construction: Q2SPOT/1 framing with its fields escaped. */
struct page { char *buf; int n, cap; };
static void put_text(struct page *p, const char *s) {
    for (; *s && p->n < p->cap - 4; ++s) {
        if (*s == '%' || *s == '\t' || *s == '\n' || *s == '\r') {
            p->n += snprintf(p->buf + p->n, p->cap - p->n, "%%%02X", (unsigned char)*s);
        } else p->buf[p->n++] = *s;
    }
    p->buf[p->n] = 0;
}
static void put_raw(struct page *p, const char *s) {
    int k = snprintf(p->buf + p->n, p->cap - p->n, "%s", s);
    p->n += k < p->cap - p->n ? k : p->cap - p->n - 1;
}
static void header(struct page *p, const char *id, const char *status, const char *message) {
    p->n = 0;
    put_raw(p, "Q2SPOT/1\n");
    put_raw(p, id);
    put_raw(p, "\t");
    put_raw(p, status);
    put_raw(p, "\t");
    put_text(p, message);
    put_raw(p, "\t\n");
}
static void row(struct page *p, const char *kind, const char *uri, const char *title, const char *sub,
                const char *context) {
    const char *fields[] = {kind, uri, title, sub, context};
    for (int i = 0; i < 5; ++i) put_text(p, fields[i]), put_raw(p, "\t");
    put_raw(p, "\n"); /* position: no held-Play/Pause op */
}

/* A small file's first line, or "" */
static void first_line(const char *file, char *out, int cap) {
    int fd = open(file, 0), n = 0;
    *out = 0;
    if (fd < 0) return;
    n = read(fd, out, cap - 1);
    close(fd);
    if (n < 0) n = 0;
    out[n] = 0;
    for (int i = 0; i < n; ++i)
        if (out[i] == '\n' || out[i] == '\r') out[i] = 0;
}

static int sha256_file(const char *file, unsigned char md[32]) {
    unsigned char buf[16384];
    unsigned len = 0;
    int fd = open(file, 0), r, ok = 0;
    if (fd < 0) return 0;
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    if (ctx && EVP_DigestInit_ex(ctx, EVP_sha256(), NULL)) {
        while ((r = read(fd, buf, sizeof buf)) > 0) EVP_DigestUpdate(ctx, buf, r);
        ok = r == 0 && EVP_DigestFinal_ex(ctx, md, &len) && len == 32;
    }
    if (ctx) EVP_MD_CTX_free(ctx);
    close(fd);
    return ok;
}
static int hex_is(const char *hex, const unsigned char md[32]) {
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 32; ++i) {
        char hi = hex[2 * i] | 0x20, lo = hex[2 * i + 1] | 0x20; /* either case */
        if (hi != digits[md[i] >> 4] || lo != digits[md[i] & 15]) return 0;
    }
    return 1;
}

/* What the Spotify slot runs: its run prefers q2-soloify, then librespot. "Our relay" by content:
 * the same as .soloify/q2-soloify, or as the copy step 3's swap recorded in backup/relay.sha256. */
static const char *slot_binary(void) {
    static const char *const names[] = {SLOT "/q2-soloify", SLOT "/librespot"};
    for (int i = 0; i < 2; ++i) {
        int fd = open(names[i], 0);
        if (fd >= 0) return close(fd), names[i];
    }
    return NULL;
}
static void slot_check(void) {
    unsigned char slot[32], ours[32];
    char recorded[80];
    if (st.slot_at && now_ms() < st.slot_at + SLOT_TTL_MS) return;
    st.slot_at = now_ms();
    first_line("VERSION", st.version, sizeof st.version);
    const char *bin = slot_binary();
    if (!bin || !sha256_file(bin, slot)) {
        st.slot = SLOT_MISSING;
        return;
    }
    if (sha256_file("q2-soloify", ours) && !memcmp(slot, ours, 32)) st.slot = SLOT_OURS;
    else {
        first_line("backup/relay.sha256", recorded, sizeof recorded);
        st.slot = strlen(recorded) >= 64 && hex_is(recorded, slot) ? SLOT_OURS_OLD : SLOT_LIBRESPOT;
    }
}
static int slot_ours(void) { return st.slot == SLOT_OURS || st.slot == SLOT_OURS_OLD; }

/* A relay of ours owns the Spotify slot's pid file: a hello would take the bridge from it. */
static int relay_running(void) {
    const char *file = getenv("SOLOIFY_RELAY_BASE");
    char line[24];
    first_line(file ? file : RELAY_BASE, line, sizeof line);
    long pid = strtol(line, NULL, 10);
    if (pid <= 0) return 0;
    if (!kill(pid, 0) || relay_errno == EPERM) return slot_ours();
    return 0;
}

static int paired(void) {
    struct endpoint eps[MAX_ENDPOINTS];
    return remote_load("remote", eps, MAX_ENDPOINTS) > 0;
}

/* The bridge check, outside any request: relay_wait keeps serving the page meanwhile. */
static void probe_run(void) {
    struct endpoint eps[MAX_ENDPOINTS];
    int n = remote_load("remote", eps, MAX_ENDPOINTS), state = BRIDGE_DOWN;
    probe.wanted = 0;
    probe.running = 1;
    if (relay_running()) state = BRIDGE_BUSY;
    for (int i = 0; i < n && state == BRIDGE_DOWN && !quitting; ++i)
        if (!relay_probe(&eps[i])) state = eps[i].lan ? BRIDGE_LAN : BRIDGE_WAN;
    say("manage: bridge %s", state == BRIDGE_LAN ? "reachable on the LAN" : state == BRIDGE_WAN ? "reachable beyond the LAN"
                             : state == BRIDGE_BUSY ? "in use by the relay" : "not reached");
    probe.state = state;
    probe.at = now_ms();
    probe.running = 0;
}

/* The choices of Delay (ms, 0 Automatic: no .delay) and Quality (kbit/s, no .quality: 160) */
static const int delays[] = {0, 1000, 2000, 4000, 6000};
static const int rates[] = {96, 128, 160};
static int delay_now(void) {
    int v = remote_setting("remote", ".delay");
    return v >= 200 && v <= 20000 ? v : 0;
}
static int quality_now(void) {
    int v = remote_setting("remote", ".quality");
    return v == 96 || v == 128 ? v : 160;
}
static void delay_text(int ms, char *out, int cap) {
    if (!ms) snprintf(out, cap, "Automatic");
    else if (ms % 1000) snprintf(out, cap, "%d ms", ms);
    else snprintf(out, cap, "%d s", ms / 1000);
}

/* A setting in folder, as Q2 Pod's own rows write it (a number, no newline; 0 removes the file),
 * written aside and renamed. */
static int setting_write(const char *folder, const char *name, int v) {
    char dest[128], tmp[144], text[16];
    snprintf(dest, sizeof dest, "%s/%s", folder, name);
    if (!v) return !unlink(dest) || relay_errno == ENOENT;
    int n = snprintf(text, sizeof text, "%d", v), fd;
    snprintf(tmp, sizeof tmp, "%s.tmp", dest);
    if ((fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644)) < 0) return 0;
    int ok = write(fd, text, n) == n;
    close(fd);
    if (ok && !rename(tmp, dest)) return 1;
    unlink(tmp);
    return 0;
}
/* Soloify's own copy, and the Spotify slot's while it runs our relay: the relay reads the one beside
 * its remote at its next connection. */
static int setting_set(const char *name, int v) {
    slot_check();
    int ok = setting_write(".", name, v);
    if (ok && slot_ours()) ok = setting_write(SLOT, name, v);
    return ok;
}

/* The first level's rows, in order. fill writes the title and subtitle, and returns 0 to leave the
 * row out.
 * Step 3 (the swap) adds, after Status, action rows "use", "update", "repair" and "restore", each
 * shown only while the slot's state (st.slot) calls for it, with ops of the same
 * names in ops[].
 * Step 5 (pairing) adds an info row "away" after "bridge", and a list row "pair" before Delay. */
struct menu_row {
    const char *kind, *uri;
    int (*fill)(char *title, char *sub, int cap);
};
static int fill_slot(char *title, char *sub, int cap) {
    snprintf(title, cap, "Spotify slot");
    if (st.slot == SLOT_MISSING) snprintf(sub, cap, "Missing");
    else if (st.slot == SLOT_LIBRESPOT) snprintf(sub, cap, "librespot");
    else if (st.slot == SLOT_OURS_OLD) snprintf(sub, cap, "Soloify (older copy)");
    else if (*st.version) snprintf(sub, cap, "Soloify v%s", st.version);
    else snprintf(sub, cap, "Soloify");
    return 1;
}
static int fill_paired(char *title, char *sub, int cap) {
    snprintf(title, cap, "Paired");
    snprintf(sub, cap, "%s", paired() ? "Yes" : "No");
    return 1;
}
static int fill_bridge(char *title, char *sub, int cap) {
    static const char *const words[] = {"Checking…", "Reachable (LAN)", "Reachable (WAN)", "Not reached",
                                        "In use by Soloify"};
    if (!paired()) return 0;
    snprintf(title, cap, "Bridge");
    snprintf(sub, cap, "%s", words[probe.state]);
    return 1;
}
static int fill_delay(char *title, char *sub, int cap) {
    snprintf(title, cap, "Delay");
    delay_text(delay_now(), sub, cap);
    return 1;
}
static int fill_quality(char *title, char *sub, int cap) {
    snprintf(title, cap, "Quality");
    snprintf(sub, cap, "%d kbit/s", quality_now());
    return 1;
}
static const struct menu_row menu_rows[] = {
    {"info", "slot", fill_slot},
    {"info", "paired", fill_paired},
    {"info", "bridge", fill_bridge},
    {"list", "delay", fill_delay},
    {"list", "quality", fill_quality},
};

/* The ops: arg is the request's first argument, or NULL. Each writes the whole reply. */
static void op_menu(struct page *p, const char *id, const char *arg) {
    char title[96], sub[96];
    slot_check();
    if (paired() && !probe.running && (!probe.at || now_ms() >= probe.at + PROBE_TTL_MS)) probe.wanted = 1;
    /* refresh while the check runs, so its answer shows */
    header(p, id, "ok", probe.wanted || probe.running ? "refresh" : "");
    for (unsigned i = 0; i < sizeof menu_rows / sizeof *menu_rows; ++i)
        if (menu_rows[i].fill(title, sub, sizeof title))
            row(p, menu_rows[i].kind, menu_rows[i].uri, title, sub, "");
}
/* A choice list: each an action row of op with the value as its context, the current one marked.
 * refresh makes the mark move at once after a press. */
static void choices(struct page *p, const char *id, const char *op, const int *values, int n, int now,
                    int delay) {
    char title[32], value[16];
    header(p, id, "ok", "refresh");
    for (int i = 0; i < n; ++i) {
        if (delay) delay_text(values[i], title, sizeof title);
        else snprintf(title, sizeof title, "%d kbit/s", values[i]);
        snprintf(value, sizeof value, "%d", values[i]);
        row(p, "action", op, title, values[i] == now ? "Selected" : "", value);
    }
}
static int choice(const char *arg, const int *values, int n) {
    if (!arg || !*arg) return -1;
    for (const char *c = arg; *c; ++c)
        if (*c < '0' || *c > '9') return -1;
    int v = strtol(arg, NULL, 10);
    for (int i = 0; i < n; ++i)
        if (values[i] == v) return v;
    return -1;
}
static void op_delay(struct page *p, const char *id, const char *arg) {
    choices(p, id, "setdelay", delays, 5, delay_now(), 1);
}
static void op_quality(struct page *p, const char *id, const char *arg) {
    choices(p, id, "setquality", rates, 3, quality_now(), 0);
}
/* An empty message: the choice list is asked again at once, so its mark moves. */
static void op_setdelay(struct page *p, const char *id, const char *arg) {
    int v = choice(arg, delays, 5);
    if (v < 0) header(p, id, "bad", "Not a delay");
    else if (!setting_set(".delay", v)) header(p, id, "error", "Couldn't save the delay");
    else header(p, id, "ok", "");
}
static void op_setquality(struct page *p, const char *id, const char *arg) {
    int v = choice(arg, rates, 3);
    if (v < 0) header(p, id, "bad", "Not a quality");
    else if (!setting_set(".quality", v)) header(p, id, "error", "Couldn't save the quality");
    else header(p, id, "ok", "");
}
/* The reserved op: answer ok, then end (manage_main, once the reply is out). */
static void op_stop(struct page *p, const char *id, const char *arg) {
    stopping = 1;
    header(p, id, "ok", "");
}
/* Step 3 adds use, update, repair and restore; step 5 the pairing's ops. */
static const struct {
    const char *name;
    void (*run)(struct page *p, const char *id, const char *arg);
} ops[] = {
    {"menu", op_menu},
    {"delay", op_delay},
    {"quality", op_quality},
    {"setdelay", op_setdelay},
    {"setquality", op_setquality},
    {"stop", op_stop},
};

int manage_reply(const char *id, const char *raw, int n, char *out, int cap) {
    struct page p = {out, 0, cap};
    char op[32], arg[128];
    const char *s = memchr(raw + 9, '\t', n - 9), *end = raw + n - 1; /* q2pod.c checked the framing */
    int k = 0;
    for (++s; s < end && *s != '\t' && k < (int)sizeof op - 1;) op[k++] = *s++;
    op[k] = 0;
    k = 0;
    if (s < end && *s == '\t')
        for (++s; s < end && *s != '\t' && k < (int)sizeof arg - 1;) arg[k++] = *s++;
    arg[k] = 0;
    for (unsigned i = 0; i < sizeof ops / sizeof *ops; ++i)
        if (!strcmp(ops[i].name, op)) {
            ops[i].run(&p, id, k ? arg : NULL);
            return p.n;
        }
    header(&p, id, "bad", "Unknown request");
    return p.n;
}

int manage_main(void) {
    const char *idle_env = getenv("SOLOIFY_IDLE_MS");
    uint64_t idle = idle_env ? (uint64_t)strtol(idle_env, NULL, 10) : IDLE_MS;
    if (!q2pod_serve("/tmp/q2-soloify")) return 0; /* another manager serves the page */
    int failed = quitting;
    while (!quitting) {
        if (probe.wanted) probe_run();
        relay_wait(-1, 0, now_ms() + 250);
        if (stopping && !q2pod_clients()) {
            say("manage: stopped");
            break;
        }
        if (!q2pod_clients() && now_ms() >= q2pod_last_request() + idle) {
            say("manage: idle, ending");
            break;
        }
    }
    q2pod_stop();
    return failed;
}
