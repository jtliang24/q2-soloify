/* The sink, as librespot's subprocess backend has it: the stock ffmpeg decoding the relay's Ogg into
 * /bin/sh ./aplay.sh, both in one process group so a kill stops them and aplay together. Both pipes
 * are cut to a page, so audio waits in the relay's ring, where it's counted, not in a pipe. */
#include "relay.h"

#define DECODE "/usr/bin/ffmpeg -nostdin -loglevel error -probesize 32 -analyzeduration 0 " \
               "-f ogg -i pipe:0 -ac 2 -ar 44100 -f s16le pipe:1"
#define SINK "/bin/sh ./aplay.sh"
#define PIPE_BYTES 4096

static int group = -1, decoder = -1, player = -1;
static int pcm = -1;
/* Read the owning hardware substream, not an estimate of what the decoder accepted.
 * The proc root override also lets host tests exercise real parsing and ownership. */
static unsigned last_rate;
static uint64_t last_pointer;
static int pointer_seen, pointer_bad;
static char hardware[256];

static int proc_read(const char *path, char *buf, int cap) {
    int fd = open(path, 0), n;
    if (fd < 0) return 0;
    n = read(fd, buf, cap - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return 1;
}
static uint64_t proc_number(const char *buf, const char *key) {
    int k = strlen(key);
    if (k && key[k - 1] == ':') --k;
    const char *p = buf;
    uint64_t v = 0;
    while (*p && (strncmp(p, key, k) || (p[k] != ':' && p[k] != ' ' && p[k] != '\t'))) {
        const char *nl = strchr(p, '\n');
        if (!nl) return 0;
        p = nl + 1;
    }
    if (!*p) return 0;
    p += k;
    while (*p == ' ' || *p == '\t' || *p == ':') ++p;
    while (*p >= '0' && *p <= '9') v = v * 10 + *p++ - '0';
    return v;
}
int sink_rendered(uint64_t *frames, unsigned *rate) {
    char path[256], list[8192], status[2048], params[2048];
    const char *root = getenv("SOLOIFY_ASOUND");
    if (!root || !*root) root = "/proc/asound";
    if (group <= 0 || pointer_bad) return 0;
    snprintf(path, sizeof path, "%s/pcm", root);
    if (!proc_read(path, list, sizeof list)) return 0;
    int found = 0;
    uint64_t cursor = 0;
    unsigned hz = 0;
    char selected[256] = "";
    for (char *line = list; line && *line;) {
        char *end = strchr(line, '\n');
        if (end) *end = 0;
        char *p;
        int card = strtol(line, &p, 10);
        if (*p == '-') {
            int dev = strtol(p + 1, &p, 10);
            if (*p == ':' && strstr(p, "playback")) {
                char *playback = strstr(p, "playback") + 8;
                int subs = strtol(playback, NULL, 10);
                if (subs > 32) subs = 32;
                for (int sub = 0; sub < subs; ++sub) {
                    snprintf(path, sizeof path, "%s/card%d/pcm%dp/sub%d/status", root, card, dev, sub);
                    if (!proc_read(path, status, sizeof status)) continue;
                    int owner = proc_number(status, "owner_pid:");
                    if (!owner || getpgid(owner) != group) continue;
                    /* XRUN, SUSPENDED and pointer resets invalidate this playback epoch. */
                    if (!strstr(status, "state: RUNNING") && !strstr(status, "state: PREPARED")) {
                        pointer_bad = 1;
                        return 0;
                    }
                    if (!strstr(status, "hw_ptr")) return 0;
                    cursor = proc_number(status, "hw_ptr:");
                    snprintf(path, sizeof path, "%s/card%d/pcm%dp/sub%d/hw_params", root, card, dev, sub);
                    if (!proc_read(path, params, sizeof params)) return 0;
                    hz = proc_number(params, "rate:");
                    snprintf(selected, sizeof selected, "%d/%d/%d/%d", card, dev, sub, owner);
                    ++found;
                }
            }
        }
        line = end ? end + 1 : NULL;
    }
    if (found != 1 || hz < 8000 || hz > 384000) return 0;
    if (pointer_seen && (hz != last_rate || strcmp(selected, hardware) || cursor < last_pointer)) {
        pointer_bad = 1;
        return 0;
    }
    pointer_seen = 1;
    last_pointer = cursor;
    last_rate = hz;
    memcpy(hardware, selected, strlen(selected) + 1);
    *frames = cursor;
    *rate = hz;
    return 1;
}

static const char *env_or(const char *name, const char *dflt) {
    const char *v = getenv(name);
    return v && *v ? v : dflt;
}

static int spawn(const char *cmd, int in, int out, int pgid, int *close_fds, int n) {
    int parent = getpid();
    int pid = fork();
    if (pid) {
        if (pid > 0) setpgid(pid, pgid ? pgid : pid);
        return pid;
    }
    /* Also release the PCM after SIGKILL, which firmware uses for Stop Streaming Services.
     * The shell execs the command so the parent-death signal reaches the actual decoder/player. */
    prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0);
    if (getppid() != parent) _exit(0);
    setpgid(0, pgid);
    dup2(in, 0);
    if (out >= 0) dup2(out, 1);
    for (int i = 0; i < n; ++i) close(close_fds[i]);
    char exec_cmd[2048];
    if (snprintf(exec_cmd, sizeof exec_cmd, "exec %s", cmd) >= (int)sizeof exec_cmd) _exit(127);
    execl(env_or("SOLOIFY_SH", "/bin/sh"), "sh", "-c", exec_cmd, (char *)NULL);
    _exit(127);
}

int sink_start(void) {
    if (player > 0 && pcm >= 0) return sink_next(-1);
    if (player > 0) sink_stop();
    pointer_seen = pointer_bad = 0;
    int a[2], b[2];
    if (pipe(a) < 0) return -1;
    if (pipe(b) < 0) {
        close(a[0]), close(a[1]);
        return -1;
    }
    fcntl(a[1], F_SETPIPE_SZ, PIPE_BYTES);
    fcntl(b[1], F_SETPIPE_SZ, PIPE_BYTES);
    int fds[4] = {a[0], a[1], b[0], b[1]};
    decoder = spawn(env_or("SOLOIFY_DECODE", DECODE), a[0], b[1], 0, fds, 4);
    group = decoder;
    player = decoder > 0 ? spawn(env_or("SOLOIFY_SINK", SINK), b[0], -1, group, fds, 4) : -1;
    close(a[0]), close(b[0]);
    pcm = b[1];
    fcntl(pcm, F_SETFD, FD_CLOEXEC);
    if (decoder < 0 || player < 0) {
        close(a[1]);
        sink_stop();
        return -1;
    }
    fcntl(a[1], F_SETFD, FD_CLOEXEC);
    fcntl(a[1], F_SETFL, fcntl(a[1], F_GETFL) | O_NONBLOCK);
    return a[1];
}

void sink_finish(int fd) {
    int status, r = 0;
    uint64_t began = now_ms();
    int had_decoder = decoder > 0;
    if (fd >= 0) close(fd); /* EOF drains the old decoder and its resampler */
    if (decoder > 0) {
        uint64_t end = now_ms() + 250;
        while (!(r = waitpid(decoder, &status, WNOHANG)) && now_ms() < end && !quitting)
            poll(NULL, 0, 5);
        if (!r) {
            say("decoder drainage deadline");
            kill(decoder, SIGKILL);
            while (waitpid(decoder, NULL, 0) < 0 && relay_errno == EINTR) {}
        }
        decoder = -1;
    }
    if (had_decoder && now_ms() - began > 50)
        say("decoder drained in %u ms", (unsigned)(now_ms() - began));
}

int sink_player(void) { return player > 0; }

int sink_next(int fd) {
    int a[2];
    sink_finish(fd);
    if (pipe(a) < 0) return -1;
    fcntl(a[1], F_SETPIPE_SZ, PIPE_BYTES);
    int fds[3] = {a[0], a[1], pcm};
    decoder = spawn(env_or("SOLOIFY_DECODE", DECODE), a[0], pcm, group, fds, 3);
    close(a[0]);
    if (decoder < 0) { close(a[1]); return -1; }
    fcntl(a[1], F_SETFD, FD_CLOEXEC);
    fcntl(a[1], F_SETFL, fcntl(a[1], F_GETFL) | O_NONBLOCK);
    return a[1];
}

void sink_stop(void) {
    if (pcm >= 0) close(pcm);
    pcm = -1;
    if (group > 0) kill(-group, SIGKILL);
    if (decoder > 0) while (waitpid(decoder, NULL, 0) < 0 && relay_errno == EINTR) {}
    if (player > 0) while (waitpid(player, NULL, 0) < 0 && relay_errno == EINTR) {}
    group = decoder = player = -1;
}

int sink_exited(void) {
    int status;
    if (decoder > 0 && waitpid(decoder, &status, WNOHANG) == decoder) {
        decoder = -1;
        if (status) return 1;
    }
    return player > 0 && waitpid(player, NULL, WNOHANG) == player;
}
