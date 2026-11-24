/* q2-soloify --manage: the Soloify app's page on upstream Q2 Pod (a card app's Q2SPOT/1 page, served
 * on q2pod.c's browse socket at /tmp/q2-soloify, or SOLOIFY_Q2POD). card/run starts it in .soloify,
 * so ./remote, ./.delay and ./.quality are Soloify's own, and ../.spotify is the Spotify slot.
 *
 * The first level (op "menu") is menu_rows: Status (what the Spotify slot holds, whether a remote
 * is there, whether the bridge answers a ping, whether its WAN line has worked), Pair with server
 * (pair.c's run, shown as a level), the swap's rows as the slot calls for them (Use, Update, Repair,
 * Restore: swap.c), then Delay and Quality, each a list of choices.
 * The page ends itself after IDLE_MS without a request, and at the reserved op "stop".
 *
 * It runs on the Q2 only (card/run): the M1 Plus's firmware has its own Soloify row and settings.
 *
 * Paths for tests: SOLOIFY_RELAY_BASE (/tmp/q2-librespot, the
 * Spotify slot's pid file), SOLOIFY_IDLE_MS and SOLOIFY_CAPS (/tmp/q2-addon.caps). */
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
/* A row with a position: a field's text to open with (caps word fieldtext) */
static void row_at(struct page *p, const char *kind, const char *uri, const char *title, const char *sub,
                   const char *context, const char *position) {
    const char *fields[] = {kind, uri, title, sub, context};
    for (int i = 0; i < 5; ++i) put_text(p, fields[i]), put_raw(p, "\t");
    put_text(p, position);
    put_raw(p, "\n");
}
static void row(struct page *p, const char *kind, const char *uri, const char *title, const char *sub,
                const char *context) {
    row_at(p, kind, uri, title, sub, context, ""); /* position: no held-Play/Pause op */
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
static int hex_value(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    c |= 0x20;
    return c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
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
 * the same as .soloify/q2-soloify, or as the copy swap.c recorded in backup/relay.sha256. */
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
    /* ping leaves a playing relay its session. A bridge without it closes in silence: then a
     * hello, which would take the bridge from our relay, only while none of ours runs. */
    int busy = relay_running();
    for (int i = 0; i < n && state == BRIDGE_DOWN && !quitting; ++i) {
        int r = relay_ping(&eps[i]);
        if (!r || (r > 0 && !busy && !relay_probe(&eps[i]))) state = eps[i].lan ? BRIDGE_LAN : BRIDGE_WAN;
        else if (r > 0 && busy) state = BRIDGE_BUSY;
    }
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
 * Status ends with the info row "away" (pairing, step 5); then come the list row "pair", and the
 * swap's action rows "use", "update", "repair" and "restore", each shown only while the slot's
 * state (st.slot, swap_rows) calls for it, with ops of the same names in ops[]. */
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
/* The swap's rows, as swap_rows finds them for the slot's state, once per slot_check */
static int swap_shown(int which) {
    static uint64_t at;
    static int rows;
    if (at != st.slot_at) at = st.slot_at, rows = swap_rows(slot_ours(), st.slot == SLOT_OURS_OLD);
    return rows & which;
}
static int fill_use(char *title, char *sub, int cap) {
    snprintf(title, cap, "Use Soloify for Spotify");
    snprintf(sub, cap, "Spotify's librespot is kept");
    return swap_shown(SWAP_USE);
}
static int fill_update(char *title, char *sub, int cap) {
    snprintf(title, cap, "Update Spotify's copy");
    if (*st.version) snprintf(sub, cap, "To Soloify v%s", st.version);
    else *sub = 0;
    return swap_shown(SWAP_UPDATE);
}
static int fill_repair(char *title, char *sub, int cap) {
    snprintf(title, cap, "Repair");
    snprintf(sub, cap, "Spotify was updated: use Soloify again");
    return swap_shown(SWAP_REPAIR);
}
static int fill_restore(char *title, char *sub, int cap) {
    snprintf(title, cap, "Restore Spotify");
    snprintf(sub, cap, "Put librespot back");
    return swap_shown(SWAP_RESTORE);
}
/* A unix time as a date, UTC: "24 Nov 2026" */
static void date_text(long long t, char *out, int cap) {
    static const char months[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    /* days to a civil date (H. Hinnant's), in 32 bits: MIPS32 has no 64-bit division here */
    unsigned z = (unsigned)t / 86400 + 719468, era = z / 146097;
    unsigned doe = z - era * 146097, yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100), mp = (5 * doy + 2) / 153;
    unsigned d = doy - (153 * mp + 2) / 5 + 1, m = mp < 10 ? mp + 3 : mp - 9;
    snprintf(out, cap, "%u %.3s %u", d, months + 3 * (m - 1), yoe + era * 400 + (m <= 2));
}
/* Whether the WAN line has worked from here (pair.c): the relay's record in either folder */
static int fill_away(char *title, char *sub, int cap) {
    static const char *const dirs[] = {".", SLOT};
    char date[24];
    long long when;
    if (!paired()) return 0;
    snprintf(title, cap, "Away");
    switch (away_state("remote", dirs, 2, &when)) {
    case AWAY_NONE: snprintf(sub, cap, "Not set up"); break;
    case AWAY_VERIFIED: date_text(when, date, sizeof date), snprintf(sub, cap, "Verified %s", date); break;
    case AWAY_UNREACHED: snprintf(sub, cap, "Not reached: fix the address in setup, then pair again"); break;
    default: snprintf(sub, cap, "Not yet verified");
    }
    return 1;
}
static int fill_pair(char *title, char *sub, int cap) {
    snprintf(title, cap, "Pair with server");
    snprintf(sub, cap, "%s", pair_waiting() ? "Pairing…" : "Shows a code to type on the server");
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
    {"info", "away", fill_away},
    {"list", "pair", fill_pair},
    {"action", "use", fill_use},
    {"action", "update", fill_update},
    {"action", "repair", fill_repair},
    {"action", "restore", fill_restore},
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
    pair.cancel = 1;
    header(p, id, "ok", "");
}
/* The swap (swap.c): Use, Update and Repair all put the relay in place; the level, asked again,
 * shows the slot as it now is. */
static void swap_reply(struct page *p, const char *id, int in) {
    char msg[120];
    int ok = in ? swap_in(msg, sizeof msg) : swap_out(msg, sizeof msg);
    st.slot_at = 0;
    header(p, id, ok ? "ok" : "error", msg);
}
static void op_use(struct page *p, const char *id, const char *arg) { swap_reply(p, id, 1); }
static void op_restore(struct page *p, const char *id, const char *arg) { swap_reply(p, id, 0); }

/* Pair with server (op "pair"): a pairing run (pair.c) in manage_main's loop, this level showing it.
 * Entering the level starts one unless one is under way or the level was on show moments ago; while
 * it waits the level says refresh, asked again every 2 s, its rows of the same kinds, uris and
 * contexts so the firmware writes them in place. Without an answer to the broadcast, or after a
 * failure, a field row (caps word field) takes the address; Try again starts afresh. */
#define PAIR_SEEN_MS 5000 /* the level asked again within this: still on show */
#define CAPS "/tmp/q2-addon.caps"
static struct {
    int wanted;                /* a run to start, at want_host:want_port, or by broadcast */
    char want_host[128], want_port[8];
    uint64_t seen;             /* the level's last request */
} pairing;
/* Whether the firmware's caps (SOLOIFY_CAPS for tests) has word */
static int cap(const char *word) {
    const char *file = getenv("SOLOIFY_CAPS");
    char buf[512];
    int n = strlen(word), fd = open(file ? file : CAPS, 0), len = 0;
    if (fd < 0) return 0;
    len = read(fd, buf, sizeof buf - 1);
    close(fd);
    buf[len > 0 ? len : 0] = 0;
    for (char *s = buf; (s = strstr(s, word)); s += n)
        if ((s == buf || s[-1] == ' ' || s[-1] == '\n') && (!s[n] || s[n] == ' ' || s[n] == '\n')) return 1;
    return 0;
}
static void pair_start(const char *host, const char *port) {
    if (pair_waiting()) pair.cancel = 1;
    pairing.wanted = 1;
    snprintf(pairing.want_host, sizeof pairing.want_host, "%s", host ? host : "");
    snprintf(pairing.want_port, sizeof pairing.want_port, "%s", port ? port : "");
    pair.state = host ? PAIR_CONNECTING : PAIR_LOOKING;
    if (host) snprintf(pair.host, sizeof pair.host, "%s", host);
}
static void pair_page(struct page *p, const char *id) {
    char title[64], sub[192], last[128] = "";
    int waiting = pairing.wanted || pair_waiting(), field = 0, again = 0;
    struct endpoint ep;
    pairing.seen = now_ms();
    header(p, id, "ok", waiting ? "refresh" : "");
    *sub = 0;
    switch (waiting && pair.state == PAIR_IDLE ? PAIR_LOOKING : pair.state) {
    case PAIR_CONNECTING:
        snprintf(title, sizeof title, "Connecting…");
        snprintf(sub, sizeof sub, "%s", pair.host);
        break;
    case PAIR_CODE:
        snprintf(title, sizeof title, "Enter %s", pair.code);
        snprintf(sub, sizeof sub, "on your Soloify server");
        break;
    case PAIR_PAIRED:
        snprintf(title, sizeof title, "Paired");
        snprintf(sub, sizeof sub, "with %s", pair.host);
        break;
    case PAIR_NOT_FOUND:
        snprintf(title, sizeof title, "No server answered");
        snprintf(sub, sizeof sub, "Run soloify pair-device on it, or enter its address");
        field = again = 1;
        break;
    case PAIR_DENIED:
    case PAIR_FAILED:
        snprintf(title, sizeof title, "Not paired");
        snprintf(sub, sizeof sub, "%s", pair.why);
        field = pair.state == PAIR_FAILED, again = 1;
        break;
    default:
        snprintf(title, sizeof title, "Looking for your server…");
        snprintf(sub, sizeof sub, "Start soloify pair-device on it");
    }
    row(p, "info", "pairstate", title, sub, "");
    if (field && cap("field")) {
        /* The address last tried, else the paired bridge's LAN one */
        if (*pair.host) snprintf(last, sizeof last, "%s", pair.host);
        else if (remote_load("remote", &ep, 1) > 0) snprintf(last, sizeof last, "%s", ep.host);
        row_at(p, "field", "pairaddr", "Server address:", *last ? last : "Its IP address or name", "",
               cap("fieldtext") ? last : "");
    }
    if (again) row(p, "action", "pairagain", pair.state == PAIR_NOT_FOUND ? "Look again" : "Try again", "", "");
}
static void op_pair(struct page *p, const char *id, const char *arg) {
    if (!pairing.wanted && !pair_waiting() &&
        (pair.state == PAIR_IDLE || now_ms() >= pairing.seen + PAIR_SEEN_MS))
        pair_start(NULL, NULL);
    pair_page(p, id);
}
/* An action: the level is asked again at once, showing the new run */
static void op_pairagain(struct page *p, const char *id, const char *arg) {
    pair_start(NULL, NULL);
    header(p, id, "ok", "");
}
/* The field's text: HOST or HOST:PORT, its escapes undone. The reply is the level's own. */
static void op_pairaddr(struct page *p, const char *id, const char *arg) {
    char host[128], *port;
    int n = 0;
    for (const char *s = arg ? arg : ""; *s && n < (int)sizeof host - 1; ++s) {
        int hi, lo;
        if (*s == '%' && s[1] && s[2] && (hi = hex_value(s[1])) >= 0 && (lo = hex_value(s[2])) >= 0)
            host[n++] = hi << 4 | lo, s += 2;
        else host[n++] = *s;
    }
    host[n] = 0;
    while (n && host[n - 1] == ' ') host[--n] = 0;
    char *h = host;
    while (*h == ' ') ++h;
    if ((port = strchr(h, ':'))) *port++ = 0;
    int ok = *h && (!port || (*port && strlen(port) <= 5));
    for (char *c = h; ok && *c; ++c)
        ok = (*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') || (*c >= '0' && *c <= '9') || *c == '.' || *c == '-';
    for (char *c = port; ok && c && *c; ++c) ok = *c >= '0' && *c <= '9';
    if (ok && port && (strtol(port, NULL, 10) < 1 || strtol(port, NULL, 10) > 65535)) ok = 0;
    if (!ok) {
        header(p, id, "bad", "Not an address: enter an IP address or name");
        return;
    }
    pair_start(h, port);
    pair_page(p, id);
}
/* manage_main's part: a run asked for, outside any request */
static void pair_serve(void) {
    if (!pairing.wanted) return;
    pairing.wanted = 0;
    slot_check();
    pair_run("remote", slot_ours() ? SLOT : NULL, *pairing.want_host ? pairing.want_host : NULL,
             pairing.want_port);
    if (pair.state == PAIR_PAIRED) probe.at = 0; /* check the bridge it gave */
}

static const struct {
    const char *name;
    void (*run)(struct page *p, const char *id, const char *arg);
} ops[] = {
    {"menu", op_menu},
    {"delay", op_delay},
    {"quality", op_quality},
    {"setdelay", op_setdelay},
    {"setquality", op_setquality},
    {"use", op_use},
    {"update", op_use},
    {"repair", op_use},
    {"restore", op_restore},
    {"pair", op_pair},
    {"pairagain", op_pairagain},
    {"pairaddr", op_pairaddr},
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
        pair_serve();
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
