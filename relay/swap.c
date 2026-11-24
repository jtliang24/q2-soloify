/* The swap: the manager (manage.c) puts the relay in the Spotify slot's place, and takes it out
 * again. It runs in .soloify, so ../.spotify is the slot, and as the card's user, uid 1000.
 *
 * In: stop Spotify, back up the slot's librespot, run and aplay.sh into backup/ once (with a
 * SOURCE line: librespot's size and SHA-256), copy aplay.sh, run, remote, .delay and .quality
 * into the slot, then the relay as the slot's librespot last, and record its hash in
 * backup/relay.sha256 (a swap in place; manage.c knows an older relay of ours by it). Each copy
 * goes to a temporary name, is synced and renamed, so a cut leaves temporary files and the old
 * binary, and the next try mends it. The slot's cache and .cache-size are never touched.
 *
 * Out: stop Spotify, copy the backup's files back (librespot last), and delete what the swap put
 * there; backup/ stays for the next swap. Without a backup, only our files go.
 *
 * For tests: SOLOIFY_RELAY_BASE (/tmp/q2-librespot, the Spotify slot's pid file) and
 * SOLOIFY_LIBRESPOT (librespot, the process name a leftover is killed by). */
#include "relay.h"

#define SLOT "../.spotify"
#define RELAY_BASE "/tmp/q2-librespot"
#define STOP_MS 3000 /* the slot's loop has this long to remove its pid file after a TERM */
#define LEFTOVER_MS 1000
#define MARK "SOLOIFY_PARENT" /* in every relay since v0.0.1 (main.c's getenv), never in librespot */

static void sleep_ms(int ms) { poll(NULL, 0, ms); }
static int exists(const char *file) {
    int fd = open(file, O_RDONLY);
    return fd >= 0 && !close(fd);
}
static int gone(const char *file) { return !unlink(file) || relay_errno == ENOENT; }

/* A file's SHA-256 and size, and whether it holds MARK (a relay of ours, recorded or not) */
static int hash_file(const char *file, unsigned char md[32], long *size, int *marked) {
    static const char mark[] = MARK;
    const int m = sizeof mark - 1;
    unsigned char buf[16384 + sizeof mark];
    unsigned len = 0;
    int fd = open(file, O_RDONLY), r, keep = 0, ok = 0;
    if (size) *size = 0;
    if (marked) *marked = 0;
    if (fd < 0) return 0;
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    if (ctx && EVP_DigestInit_ex(ctx, EVP_sha256(), NULL)) {
        /* the last m - 1 bytes are kept in front of the next read, for a mark across two reads */
        while ((r = read(fd, buf + keep, 16384)) > 0) {
            EVP_DigestUpdate(ctx, buf + keep, r);
            if (size) *size += r;
            int n = keep + r;
            for (int i = 0; marked && !*marked && i + m <= n; ++i)
                if (buf[i] == 'S' && !memcmp(buf + i, mark, m)) *marked = 1;
            keep = n < m - 1 ? n : m - 1;
            memmove(buf, buf + n - keep, keep);
        }
        ok = r == 0 && EVP_DigestFinal_ex(ctx, md, &len) && len == 32;
    }
    if (ctx) EVP_MD_CTX_free(ctx);
    close(fd);
    return ok;
}
static void hex(const unsigned char md[32], char out[65]) {
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 32; ++i) out[2 * i] = digits[md[i] >> 4], out[2 * i + 1] = digits[md[i] & 15];
    out[64] = 0;
}
/* The first 64 characters of a small file's first word, lower case, or "" */
static void read_hex(const char *file, int skip_words, char out[65]) {
    char buf[200];
    int fd = open(file, O_RDONLY), n = fd < 0 ? 0 : read(fd, buf, sizeof buf - 1), k = 0;
    if (fd >= 0) close(fd);
    buf[n < 0 ? 0 : n] = 0;
    const char *s = buf;
    for (int w = 0; w < skip_words; ++w) {
        while (*s && *s != ' ') ++s;
        while (*s == ' ') ++s;
    }
    for (; k < 64 && ((*s >= '0' && *s <= '9') || ((*s | 0x20) >= 'a' && (*s | 0x20) <= 'f')); ++s)
        out[k++] = *s | (*s >= 'A' ? 0x20 : 0);
    out[k == 64 ? 64 : 0] = 0;
}

/* to by way of to.tmp: written, synced and renamed. A missing from fails. */
static int write_file(const char *to, const void *data, int n, const char *from, int mode) {
    char tmp[160], buf[16384];
    int in = -1, out, r = 0, ok = 1;
    if (from && (in = open(from, O_RDONLY)) < 0) return 0;
    snprintf(tmp, sizeof tmp, "%s.tmp", to);
    if ((out = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, mode)) < 0) {
        if (in >= 0) close(in);
        return 0;
    }
    if (from)
        while (ok && (r = read(in, buf, sizeof buf)) > 0) ok = write(out, buf, r) == r;
    else ok = write(out, data, n) == n;
    ok = ok && r >= 0 && !fsync(out);
    if (in >= 0) close(in);
    ok = !close(out) && ok;
    if (ok && !rename(tmp, to)) return 1;
    say("swap: couldn't write %s", to);
    unlink(tmp);
    return 0;
}
static int copy(const char *from, const char *to, int mode) { return write_file(to, NULL, 0, from, mode); }

/* killall librespot: every process named so; 0 when one of them couldn't be signalled */
static int killall(int sig, int *found) {
    const char *name = getenv("SOLOIFY_LIBRESPOT");
    DIR *d = opendir("/proc");
    struct dirent *e;
    int ok = 1, me = getpid();
    *found = 0;
    if (!d) return 1;
    while ((e = readdir(d))) {
        char file[64], comm[32];
        int pid = strtol(e->d_name, NULL, 10), fd, n;
        if (pid <= 0 || pid == me) continue;
        snprintf(file, sizeof file, "/proc/%d/comm", pid);
        if ((fd = open(file, O_RDONLY)) < 0) continue;
        n = read(fd, comm, sizeof comm - 1);
        close(fd);
        if (n <= 0) continue;
        comm[n - (comm[n - 1] == '\n')] = 0;
        if (strcmp(comm, name ? name : "librespot")) continue;
        *found = 1;
        if (kill(pid, sig) && relay_errno == EPERM) ok = 0;
    }
    closedir(d);
    return ok;
}

#define ROOTS_SPOTIFY "Choose Stop Streaming Services, then try again"
/* Spotify stopped: its loop, by the pid in its pid file, then any librespot left. On firmware before
 * 1.9.1 Spotify runs as root, and only the firmware can stop it. */
static int spotify_stop(char *msg, int cap) {
    const char *base = getenv("SOLOIFY_RELAY_BASE");
    char line[24] = "";
    int fd, n, found;
    if (!base) base = RELAY_BASE;
    if ((fd = open(base, O_RDONLY)) >= 0) {
        n = read(fd, line, sizeof line - 1);
        close(fd);
        line[n > 0 ? n : 0] = 0;
    }
    long pid = strtol(line, NULL, 10);
    if (pid > 0 && pid != getpid()) {
        if (!kill(pid, SIGTERM)) {
            uint64_t end = now_ms() + STOP_MS;
            while (now_ms() < end && exists(base) && !kill(pid, 0)) sleep_ms(50);
            if (!kill(pid, 0)) say("swap: Spotify's loop %ld outlived its TERM", pid), kill(pid, SIGKILL);
            say("swap: stopped Spotify (%ld)", pid);
        } else if (relay_errno == EPERM) {
            say("swap: Spotify's loop %ld isn't ours to stop", pid);
            snprintf(msg, cap, ROOTS_SPOTIFY);
            return 0;
        }
    }
    if (!killall(SIGTERM, &found)) {
        snprintf(msg, cap, ROOTS_SPOTIFY);
        return 0;
    }
    for (uint64_t end = now_ms() + LEFTOVER_MS; found && now_ms() < end;) {
        sleep_ms(50);
        killall(0, &found);
    }
    if (found) killall(SIGKILL, &found);
    return 1;
}

/* Our relay, by content: .soloify's, the copy the last swap recorded, or any relay (MARK) */
static int relay_like(const char *file, const unsigned char ours[32]) {
    unsigned char md[32];
    char recorded[65], have[65];
    int marked;
    if (!hash_file(file, md, NULL, &marked)) return 0;
    if (marked || !memcmp(md, ours, 32)) return 1;
    read_hex("backup/relay.sha256", 0, recorded);
    hex(md, have);
    return *recorded && !strcmp(recorded, have);
}

/* The backup is whole: SOURCE names backup/librespot's size and hash */
static int backup_whole(void) {
    unsigned char md[32];
    char want[65], have[65];
    long size;
    read_hex("backup/SOURCE", 2, want);
    if (!*want || !hash_file("backup/librespot", md, &size, NULL)) return 0;
    hex(md, have);
    return !strcmp(want, have);
}

/* The slot's librespot, run and aplay.sh into backup/, once for each librespot, and never when
 * that librespot is a relay: a backup of real librespot is never replaced by one. SOURCE goes first and comes back last, so a backup
 * is only ever taken whole. */
static int backup(const unsigned char ours[32]) {
    static const char *const names[] = {"run", "aplay.sh"};
    unsigned char md[32];
    char from[64], to[64], text[120], digest[65];
    long size;
    if (!exists(SLOT "/librespot") || relay_like(SLOT "/librespot", ours)) return 1;
    if (!hash_file(SLOT "/librespot", md, &size, NULL)) return 0;
    hex(md, digest);
    read_hex("backup/SOURCE", 2, text);
    /* Backed up already: a try cut short may have put our run in the slot since */
    if (!strcmp(text, digest) && backup_whole()) return 1;
    if (mkdir("backup", 0755) && relay_errno != EEXIST) return 0;
    if (!gone("backup/SOURCE")) return 0;
    for (int i = 0; i < 2; ++i) {
        snprintf(from, sizeof from, SLOT "/%s", names[i]);
        snprintf(to, sizeof to, "backup/%s", names[i]);
        if (exists(from) ? !copy(from, to, 0755) : !gone(to)) return 0;
    }
    if (!copy(SLOT "/librespot", "backup/librespot", 0755)) return 0;
    int n = snprintf(text, sizeof text, "librespot %ld %s\n", size, digest);
    say("swap: backed up librespot (%ld bytes)", size);
    return write_file("backup/SOURCE", text, n, NULL, 0644);
}

int swap_rows(int ours, int older) {
    unsigned char md[32];
    char want[65], have[65];
    if (ours) return (older ? SWAP_UPDATE : 0) | SWAP_RESTORE;
    /* A swap was in place and the slot's librespot is a new one: the App Store's Spotify Update */
    read_hex("backup/relay.sha256", 0, want);
    if (*want && backup_whole() && hash_file(SLOT "/librespot", md, NULL, NULL)) {
        read_hex("backup/SOURCE", 2, want);
        hex(md, have);
        if (strcmp(want, have)) return SWAP_REPAIR;
    }
    return SWAP_USE;
}

int swap_in(char *msg, int cap) {
    static const struct { const char *name; int mode, keep; } files[] = {
        /* keep: a file .soloify lacks stays in the slot as it is, else it goes */
        {"aplay.sh", 0755, 0}, {"run", 0755, 0}, {"remote", 0600, 1}, {".delay", 0644, 0},
        {".quality", 0644, 0},
    };
    unsigned char ours[32];
    char from[64], to[64], digest[65];
    *msg = 0;
    if (!hash_file("q2-soloify", ours, NULL, NULL) || !exists("run") || !exists("aplay.sh")) {
        snprintf(msg, cap, "Soloify's files are missing: install it again");
        return 0;
    }
    if (!spotify_stop(msg, cap)) return 0;
    if (mkdir(SLOT, 0755) && relay_errno != EEXIST) {
        snprintf(msg, cap, "Couldn't make the Spotify folder");
        return 0;
    }
    if (!backup(ours)) {
        snprintf(msg, cap, "Couldn't back up Spotify");
        return 0;
    }
    for (unsigned i = 0; i < sizeof files / sizeof *files; ++i) {
        snprintf(from, sizeof from, "%s", files[i].name);
        snprintf(to, sizeof to, SLOT "/%s", files[i].name);
        int ok = exists(from) ? copy(from, to, files[i].mode) : files[i].keep || gone(to);
        if (!ok) {
            snprintf(msg, cap, "Couldn't copy %s: try again", files[i].name);
            return 0;
        }
    }
    /* run prefers q2-soloify, and killall librespot wouldn't stop it */
    if (!copy("q2-soloify", SLOT "/librespot", 0755) || !gone(SLOT "/q2-soloify")) {
        snprintf(msg, cap, "Couldn't copy Soloify: try again");
        return 0;
    }
    hex(ours, digest);
    digest[64] = '\n';
    if ((mkdir("backup", 0755) && relay_errno != EEXIST) || !write_file("backup/relay.sha256", digest, 65, NULL, 0644))
        say("swap: couldn't record the relay's hash");
    say("swap: Soloify is in Spotify's place");
    snprintf(msg, cap, "Spotify now plays through Soloify");
    return 1;
}

int swap_out(char *msg, int cap) {
    static const char *const ours_only[] = {"q2-soloify", "remote", ".delay", ".quality"};
    unsigned char ours[32], md[32], run[32];
    char file[64];
    *msg = 0;
    if (!spotify_stop(msg, cap)) return 0;
    int whole = backup_whole();
    if (whole) {
        if ((exists("backup/run") && !copy("backup/run", SLOT "/run", 0755)) ||
            (exists("backup/aplay.sh") && !copy("backup/aplay.sh", SLOT "/aplay.sh", 0755)) ||
            !copy("backup/librespot", SLOT "/librespot", 0755)) {
            snprintf(msg, cap, "Couldn't restore Spotify: try again");
            return 0;
        }
    } else {
        /* Nothing to put back: take out only what is ours */
        if (!hash_file("q2-soloify", ours, NULL, NULL)) memset(ours, 0, sizeof ours);
        if (relay_like(SLOT "/librespot", ours) && !gone(SLOT "/librespot")) {
            snprintf(msg, cap, "Couldn't remove Soloify from Spotify's folder");
            return 0;
        }
        if (hash_file("run", run, NULL, NULL) && hash_file(SLOT "/run", md, NULL, NULL) && !memcmp(md, run, 32))
            gone(SLOT "/run");
    }
    for (unsigned i = 0; i < sizeof ours_only / sizeof *ours_only; ++i) {
        snprintf(file, sizeof file, SLOT "/%s", ours_only[i]);
        gone(file);
    }
    gone("backup/relay.sha256"); /* no swap in place: a new librespot is no reason to Repair */
    if (whole) say("swap: librespot is back"), snprintf(msg, cap, "Spotify's librespot is back");
    else say("swap: no backup"), snprintf(msg, cap, "Install Spotify from the App Store");
    return 1;
}
