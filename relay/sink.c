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

void sink_drain(void) {
    if (pcm >= 0) close(pcm);
    pcm = -1;
}

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
