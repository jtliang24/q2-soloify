/* The relay's buffer, the only one between Soloist and the DAC (docs/wire.md). Packets wait in the
 * ring until it holds the target, then feed the sink in real time, at most 300 ms ahead: a pipe
 * bounds bytes, not time, and would hold seconds of silence's small packets uncounted. An underrun
 * rebuffers to the target while the stream keeps arriving, so nothing is missed. The sink stops
 * after 5 s without audio, freeing the DAC. A ring that stays a second over its target for 30 s
 * (the two ends' clocks drift) is trimmed to it, as is a backlog that much over it on a refill. */
#include "relay.h"

#define SLOTS 1536 /* 30 s */
#define SLOT_BYTES 1276 /* Opus's largest packet, 1275 bytes */
#define SINK_IDLE_MS 5000
#define TRIM_OVER_MS 1000
#define TRIM_AFTER_MS 30000
#define LEAD_MS 300 /* what may wait in the pipes, the decoder and ALSA */

static unsigned char ring[SLOTS][SLOT_BYTES];
static short lens[SLOTS];
static int head, count, target = 1000, feeding, fd = -1;
static struct ogg ogg;
static unsigned char page[2048];
static int npage, off;
static uint64_t idle_since, over_since, fed_since;
static unsigned fed_ms;
static unsigned underruns, trims, drops, sinks;
static int enabled = 1;
static uint64_t received;

static int level_ms(void) { return count * FRAME_MS; }

static void pop(void) {
    head = (head + 1) % SLOTS;
    --count;
}

static void trim(const char *why) {
    int from = level_ms();
    while (level_ms() > target) pop();
    ++trims;
    say("trimmed %d ms to %d (%s)", from, target, why);
}

static void stop_sink(void) {
    if (fd >= 0) close(fd);
    sink_stop();
    fd = -1, feeding = 0, npage = off = 0;
}

static void start_feeding(void) {
    if (!enabled) return;
    if (fd < 0) {
        if ((fd = sink_start()) < 0) {
            say("can't start the sink");
            return;
        }
        ++sinks;
        npage = ogg_headers(&ogg, page), off = 0;
        q2pod_started();
    }
    if (level_ms() > target + TRIM_OVER_MS) trim("backlog");
    feeding = 1, fed_since = now_ms(), fed_ms = 0;
}

/* Fed as far ahead of real time as the sink may be */
static int ahead(void) { return off == npage && fed_ms > now_ms() - fed_since + LEAD_MS; }

void audio_start(int target_ms) {
    static int ready;
    if (!ready) ogg_init(), ready = 1;
    target = target_ms;
}

void audio_packet(const unsigned char *p, int n) {
    int i;
    if (n <= 0 || n > SLOT_BYTES) return;
    if (count == SLOTS) pop(), ++drops;
    i = (head + count) % SLOTS;
    memcpy(ring[i], p, n);
    lens[i] = n;
    ++count;
    received += FRAME_MS;
    if (!feeding && level_ms() >= target) start_feeding();
}

int audio_pollfd(struct pollfd *pfd) {
    if (fd < 0 || !feeding || ahead()) return 0;
    pfd->fd = fd, pfd->events = POLLOUT, pfd->revents = 0;
    return 1;
}

void audio_service(short revents) {
    if (revents & (POLLERR | POLLHUP)) {
        say("the sink went");
        stop_sink();
        return;
    }
    while (feeding) {
        int w;
        if (off == npage) {
            if (!count) {
                feeding = 0, ++underruns, idle_since = now_ms();
                return;
            }
            if (ahead()) return;
            npage = ogg_packet(&ogg, page, ring[head], lens[head]), off = 0;
            pop();
            fed_ms += FRAME_MS;
        }
        if ((w = write(fd, page + off, npage - off)) < 0) {
            if (relay_errno != EAGAIN && relay_errno != EINTR) say("the sink went"), stop_sink();
            return;
        }
        off += w;
    }
}

void audio_tick(void) {
    uint64_t now = now_ms();
    if (fd >= 0 && sink_exited()) {
        say("the sink exited");
        stop_sink();
    }
    if (fd >= 0 && !feeding && now - idle_since > SINK_IDLE_MS) stop_sink();
    if (feeding && level_ms() > target + TRIM_OVER_MS) {
        if (!over_since) over_since = now;
        else if (now - over_since > TRIM_AFTER_MS) trim("drift"), over_since = 0;
    } else {
        over_since = 0;
    }
    if (enabled && !feeding && level_ms() >= target) start_feeding();
}

void audio_enable(int on) {
    if (!on && enabled && fd >= 0) {
        uint64_t now = now_ms();
        unsigned ahead_ms = fed_ms > now - fed_since ? fed_ms - (now - fed_since) : 0;
        unsigned rewind = (ahead_ms + FRAME_MS - 1) / FRAME_MS;
        if (rewind > LEAD_MS / FRAME_MS + 1) rewind = LEAD_MS / FRAME_MS + 1;
        if (rewind > (unsigned)(SLOTS - count)) rewind = SLOTS - count;
        head = (head + SLOTS - rewind) % SLOTS;
        count += rewind; /* packets submitted ahead of the playback clock survive local pause */
    }
    enabled = on;
    if (!on) stop_sink();
}

void audio_clear(void) {
    stop_sink();
    head = count = 0;
    received = 0;
    fed_ms = 0;
    over_since = 0;
}

uint64_t audio_received(void) { return received; }
int audio_target(void) { return target; }

uint64_t audio_played(void) {
    uint64_t ahead_ms = 0, now = now_ms();
    if (fd >= 0 && fed_ms > now - fed_since) ahead_ms = fed_ms - (now - fed_since);
    return received > (uint64_t)level_ms() + ahead_ms ? received - level_ms() - ahead_ms : 0;
}

int audio_stats(char *buf, int cap) {
    return snprintf(buf, cap, "ring=%d target=%d underruns=%u trims=%u drops=%u sinks=%u", level_ms(), target,
                    underruns, trims, drops, sinks);
}
