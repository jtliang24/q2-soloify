/* The relay's bounded Opus queue. Encoder generations are decoded separately into the SAME
 * PCM player. Their padding and warmup are trimmed; explicit pause drops the output immediately. */
#include "relay.h"
#define SLOTS 1536
#define SLOT_BYTES 1276
#define SINK_IDLE_MS 5000
#define TRIM_OVER_MS 1000
#define TRIM_AFTER_MS 30000
#define LEAD_MS 300
#define HISTORY 4 /* 80 ms decoder convergence when resuming partway through a run */

struct prefix { unsigned skip; int n; unsigned char data[]; };
static unsigned char ring[SLOTS][SLOT_BYTES], page[8192];
static short lens[SLOTS], valid[SLOTS], duration[SLOTS];
static unsigned generations[SLOTS];
static unsigned char endings[SLOTS];
static struct prefix *prefixes[SLOTS];
static int head, count, target = 1000, feeding, fd = -1, npage, off, ending;
static unsigned input_gen = 1, active_gen, input_skip = 312, original_skip = 312;
static unsigned stream_serial = 1;
static unsigned end_seq, end_samples;
static int have_end, need_prefix = 1, primer_n;
static unsigned char primer[SLOT_BYTES];
static struct ogg ogg;
static uint64_t idle_since, over_since, fed_since, fed_samples, buffered, received;
static unsigned underruns, trims, drops, sinks;
static int enabled = 1, fresh_required, fresh_capture;
static uint64_t rendered_base;
static int rendered_started, rendered_invalid;
static const char *bookmark_reason = "startup";
const char *audio_clock_reason(void) { return bookmark_reason; }

/* The freestanding MIPS link has no 64-bit division runtime. The ring fits 32 bits;
 * cumulative sample clocks use two 32-bit words and a bounded long division. */
static uint64_t sample_ms(uint64_t samples) {
    unsigned hi = samples >> 32, lo = samples, rem = hi % 48, result = 0;
    for (int bit = 31; bit >= 0; --bit) {
        rem = (rem << 1) | ((lo >> bit) & 1);
        result <<= 1;
        if (rem >= 48) rem -= 48, result |= 1;
    }
    return (uint64_t)(hi / 48) << 32 | result;
}
static int level_ms(void) { return (unsigned)buffered / 48; }
static void pop(void) {
    buffered -= duration[head];
    head = (head + 1) % SLOTS;
    --count;
}
static void trim(const char *why) {
    int from = level_ms();
    while (count && level_ms() > target) pop();
    if (rendered_started) rendered_invalid = 1;
    ++trims;
    say("trimmed %d ms to %d (%s)", from, target, why);
}
static void stop_sink(void) {
    if (sink_player()) say("sink stop buffered_ms=%d", level_ms());
    if (fd >= 0) close(fd);
    sink_stop();
    fd = -1, feeding = npage = off = ending = 0;
    active_gen = 0;
    if (rendered_started) rendered_invalid = 1;
}

/* Decode genuine history rather than restarting predictive Opus state on an arbitrary
 * retained packet. A fresh run carries its own warmup and encoder pre-skip. */
static int headers(void) {
    int first = head, history = 0, n, at;
    unsigned skip = 0;
    if (!prefixes[head]) while (history < HISTORY) {
        int prev = (first + SLOTS - 1) % SLOTS;
        if (!lens[prev] || generations[prev] != generations[head]) break;
        first = prev;
        ++history;
        if (prefixes[first]) break;
    }
    struct prefix *p = prefixes[first];
    if (p) skip = p->skip;
    for (int i = 0, slot = first; i < history; ++i, slot = (slot + 1) % SLOTS)
        skip += p ? duration[slot] : valid[slot];
    if (!p) {
        /* A very rapid pause can leave head inside the hidden prefix. Its original
         * header may be beyond the history window; skip the still-hidden samples too. */
        for (int i = 0, slot = head; i < count && generations[slot] == generations[head];
             ++i, slot = (slot + 1) % SLOTS) {
            skip += valid[slot] - duration[slot];
            if (duration[slot]) break;
        }
    }
    n = ogg_headers(&ogg, page, skip);
    if (p && p->n) n += ogg_packet(&ogg, page + n, p->data, p->n, 960, 0);
    at = first;
    for (int i = 0; i < history; ++i, at = (at + 1) % SLOTS)
        n += ogg_packet(&ogg, page + n, ring[at], lens[at], valid[at], endings[at]);
    return n;
}
static int decoder_for_head(void) {
    if (active_gen == generations[head]) return 1;
    if (fd < 0 && (fd = sink_start()) < 0) { stop_sink(); return 0; }
    if (active_gen && (fd = sink_next(fd)) < 0) { stop_sink(); return 0; }
    active_gen = generations[head];
    npage = headers(), off = 0;
    say("decoder gen=%u", active_gen);
    return 1;
}
static void start_feeding(void) {
    if (!enabled || (fresh_required || !q2pod_ready()) || !count) return;
    if (level_ms() > target + TRIM_OVER_MS) trim("backlog");
    if (fd < 0) {
        int existed = sink_player();
        if ((fd = sink_start()) < 0) { say("can't start the sink"); return; }
        if (!existed) {
            rendered_base = received - buffered;
            rendered_started = 1;
            rendered_invalid = 0;
            ++sinks;
            say("sink start buffered_ms=%d target_ms=%d", level_ms(), target);
        }
        if (!decoder_for_head()) return;
        if (!existed) q2pod_started();
    } else if (!decoder_for_head()) return;
    feeding = 1, fed_since = now_ms(), fed_samples = 0;
}
static int ahead(void) {
    return off == npage && fed_samples > (now_ms() - fed_since + LEAD_MS) * 48;
}
void audio_start(int target_ms) {
    static int ready;
    if (!ready) ogg_init(), ready = 1;
    target = target_ms;
}
void audio_capture(unsigned generation, unsigned skip, const unsigned char *warmup, int n) {
    say("presentation capture=%u decoder=%u received_ms=%llu skip=%u warmup=%d epoch=%u",
        generation, stream_serial + 1, (unsigned long long)sample_ms(received), skip, n, sinks);
    /* Bridge generation numbers restart on reconnect; buffered stream identities must not. */
    if (!++stream_serial) ++stream_serial;
    input_gen = stream_serial;
    have_end = 0;
    original_skip = skip;
    input_skip = skip > (n ? 960u : 0u) ? skip - (n ? 960u : 0u) : 0;
    primer_n = n;
    if (n) memcpy(primer, warmup, n);
    need_prefix = 1;
    if (q2pod_ready()) fresh_capture = 1;
}
void audio_end(unsigned seq, unsigned samples) {
    end_seq = seq, end_samples = samples, have_end = 1;
}
void audio_packet(const unsigned char *p, int n, unsigned seq) {
    unsigned samples = have_end && end_seq == seq ? end_samples : 960;
    int end = have_end && end_seq == seq;
    have_end = 0;
    if (n <= 0 || n > SLOT_BYTES || samples > 960) return;
    unsigned skipped = input_skip < samples ? input_skip : samples;
    input_skip -= skipped;
    /* Fully skipped packets still carry the decoder history preceding the first audible
     * sample. Keep them with zero ring duration; Opus pre-skip removes their PCM. */
    if (count == SLOTS) pop(), ++drops, rendered_invalid = 1;
    int i = (head + count) % SLOTS;
    free(prefixes[i]);
    prefixes[i] = NULL;
    if (need_prefix) {
        prefixes[i] = malloc(sizeof(struct prefix) + primer_n);
        if (!prefixes[i]) { say("no memory for capture prefix"); quitting = 1; return; }
        prefixes[i]->skip = original_skip, prefixes[i]->n = primer_n;
        if (primer_n) memcpy(prefixes[i]->data, primer, primer_n);
        need_prefix = 0;
    }
    memcpy(ring[i], p, n);
    lens[i] = n, valid[i] = samples, duration[i] = samples - skipped;
    endings[i] = end;
    generations[i] = input_gen;
    ++count;
    buffered += duration[i];
    received += duration[i];
    if (enabled && fresh_capture && q2pod_ready()) fresh_required = 0;
    if (!feeding && level_ms() >= target) start_feeding();
}
int audio_pollfd(struct pollfd *pfd) {
    if (fd < 0 || !feeding || ahead()) return 0;
    pfd->fd = fd, pfd->events = POLLOUT, pfd->revents = 0;
    return 1;
}
void audio_service(short revents) {
    if (revents & (POLLERR | POLLHUP)) { say("the sink went"); stop_sink(); return; }
    while (feeding) {
        if (off == npage) {
            if (ending) {
                sink_finish(fd);
                fd = -1;
                active_gen = 0, ending = 0;
            }
            if (!count) {
                feeding = 0, idle_since = now_ms();
                ++underruns;
                return;
            }
            if (ahead()) return;
            if (active_gen != generations[head]) {
                if (!decoder_for_head()) return;
            } else {
                npage = ogg_packet(&ogg, page, ring[head], lens[head], valid[head], endings[head]);
                off = 0;
                ending = endings[head];
                fed_samples += duration[head];
                pop();
            }
        }
        int w = write(fd, page + off, npage - off);
        if (w < 0) {
            if (relay_errno != EAGAIN && relay_errno != EINTR) { say("the sink went"); stop_sink(); }
            return;
        }
        off += w;
    }
}
void audio_tick(void) {
    uint64_t now = now_ms();
    uint64_t cursor;
    unsigned hz;
    /* Observe resets/XRUNs between controls. Missing startup proc entries are allowed;
     * bookmarking itself still requires a successful hardware read. */
    if (rendered_started && sink_player()) sink_rendered(&cursor, &hz);
    if (sink_player() && sink_exited()) { say("the sink exited"); stop_sink(); }
    if (sink_player() && !feeding && now - idle_since > SINK_IDLE_MS) stop_sink();
    if (feeding && level_ms() > target + TRIM_OVER_MS) {
        if (!over_since) over_since = now;
        else if (now - over_since > TRIM_AFTER_MS) trim("drift"), over_since = 0;
    } else over_since = 0;
    if (enabled && !feeding && level_ms() >= target) start_feeding();
    if (feeding && fd < 0 && count && !ahead()) audio_service(0);
}
void audio_enable(int on) {
    if (!on) fresh_required = 1, fresh_capture = 0;
    enabled = on;
    if (!on) audio_clear();
}
void audio_pause(void) {
    say("pause drop buffered_ms=%d", level_ms());
    audio_enable(0);
}
int audio_bookmark(uint64_t *sample) {
    bookmark_reason = "valid";
    if (!rendered_started) {
        *sample = sample_ms(received - buffered);
        return 1; /* Partial startup: the first pending presentation sample. */
    }
    uint64_t frames;
    unsigned hz;
    if (rendered_invalid) { bookmark_reason = "discarded presentation samples"; return 0; }
    if (!sink_rendered(&frames, &hz)) { bookmark_reason = sink_clock_reason(); return 0; }
    /* Avoid a compiler runtime for 64-bit division on the stock MIPS link. */
    uint64_t ms = 0, rem = 0;
    for (int bit = 63; bit >= 0; --bit) {
        rem = rem * 2 + ((frames >> bit) & 1);
        ms <<= 1;
        if (rem >= hz) rem -= hz, ms |= 1;
    }
    ms = ms * 1000 + (unsigned)rem * 1000 / hz;
    *sample = sample_ms(rendered_base) + ms;
    if (*sample > audio_received()) { bookmark_reason = "pointer beyond capture timeline"; return 0; }
    return 1;
}
void audio_clear(void) {
    stop_sink();
    head = count = 0;
    received = buffered = fed_samples = over_since = 0;
    rendered_base = 0;
    rendered_started = rendered_invalid = 0;
    for (int i = 0; i < SLOTS; ++i) {
        free(prefixes[i]); prefixes[i] = NULL;
        lens[i] = 0;
    }
}
uint64_t audio_received(void) { return sample_ms(received); }
int audio_target(void) { return target; }
uint64_t audio_played(void) {
    uint64_t ahead_samples = 0, elapsed = (now_ms() - fed_since) * 48;
    if (sink_player() && fed_samples > elapsed) ahead_samples = fed_samples - elapsed;
    return received > buffered + ahead_samples ? sample_ms(received - buffered - ahead_samples) : 0;
}
int audio_stats(char *buf, int cap) {
    return snprintf(buf, cap, "ring=%d target=%d underruns=%u trims=%u drops=%u sinks=%u", level_ms(), target,
                    underruns, trims, drops, sinks);
}
