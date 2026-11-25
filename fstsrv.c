/* fstsrv: serve an FST dump on stdin/stdout without loading it.
 *
 * wavesource.py starts one of these per open dump and asks it for
 * exactly what is on screen. Opening reads only the hierarchy, so a dump of
 * any size opens at once. A read names the signals and the time window; FST
 * stores each block's changes per signal and compressed separately, so only
 * those signals' chains in the blocks overlapping the window are
 * decompressed. Zoomed out views ask for buckets instead of changes and the
 * aggregation happens here, so a summary of a long busy signal never crosses
 * the pipe as individual changes.
 *
 * Built on demand against the GTKWave FST library shipped inside Verilator
 * (<VERILATOR_ROOT>/include/gtkwave), like fst2vcd_mini.c.
 *
 * fstsrv <dump.fst> [--scope A.B.C]...   --scope lists only the variables
 * below that scope (or that one variable), for a dump with too many to list.
 *
 * Protocol (text lines). After start:
 *     wavesrv 1
 *     timescale <exponent>              e.g. -12 for 1ps
 *     range <start> <end>
 *     var <handle> <width> <full.dotted.name>[\t<type> <dir>]   once per variable; after the
 *                                                   tab its declared type (wire, reg, logic...) and
 *                                                   port direction (in, out, inout), - if unknown
 *     end
 * Commands and answers:
 *     read <t0> <t1> <h> [<h>...]
 *         p <h> <bits>                  value held before t0 (x before the dump)
 *         c <t> <h> <bits>              each real change with t0 <= t <= t1
 *         .
 *     agg <t0> <level> <nbuckets> <h> [<h>...]
 *         p <h> <bits>
 *         b <h> <bucket> <first_t> <last_t> <changes> <to_one> <last_bits>
 *         .                             buckets are 2^level wide from t0
 *     at <t> <h> [<h>...]
 *         v <h> <bits>                  value at t (after the changes at t)
 *         .
 *     stat
 *         s <size> <mtime seconds>      of the dump file, to notice a rewrite
 *     quit
 * Errors answer "! <message>". Real and string variables are not served.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <inttypes.h>
#include <sys/stat.h>

#include "fstapi.h"

#define MAX_LINE (1 << 20)

static void *ctx;
static fstHandle max_handle;
static uint32_t *width_of; /* per handle, 0 = not served */

/* per requested handle state */
struct hstate {
    int active;
    char *last;        /* last emitted value */
    /* aggregation */
    int64_t bucket;
    uint64_t first_t, last_t;
    uint64_t changes, to_one;
    char *bucket_last;
};
static struct hstate *hs;

static int mode_agg;
static uint64_t q_t0, q_t1;
static int q_level;

static void out_value(const char *v, uint32_t w)
{
    fwrite(v, 1, w, stdout);
}

static void flush_bucket(fstHandle h)
{
    struct hstate *s = &hs[h];
    if (s->bucket < 0 || s->changes == 0)
        return;
    printf("b %u %" PRId64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " ", h, s->bucket, s->first_t, s->last_t,
           s->changes, s->to_one);
    out_value(s->bucket_last, width_of[h]);
    putchar('\n');
    s->changes = 0;
    s->to_one = 0;
}

static void on_change(void *user, uint64_t t, fstHandle h, const unsigned char *value)
{
    (void)user;
    if (h > max_handle || !hs[h].active)
        return;
    if (t < q_t0 || t > q_t1)
        return;
    struct hstate *s = &hs[h];
    uint32_t w = width_of[h];
    if (memcmp(s->last, value, w) == 0)
        return; /* rewrite of the same value (block start frames, aliases) */
    memcpy(s->last, value, w);
    if (!mode_agg) {
        printf("c %" PRIu64 " %u ", t, h);
        out_value((const char *)value, w);
        putchar('\n');
        return;
    }
    int64_t b = (int64_t)((t - q_t0) >> q_level);
    if (b != s->bucket) {
        flush_bucket(h);
        s->bucket = b;
        s->first_t = t;
    }
    s->last_t = t;
    s->changes++;
    if (w == 1 && value[0] == '1')
        s->to_one++;
    memcpy(s->bucket_last, value, w);
}

static void on_change_varlen(void *user, uint64_t t, fstHandle h, const unsigned char *value, uint32_t len)
{
    (void)len;
    on_change(user, t, h, value);
}

static char *valbuf;

static void prior(fstHandle h, uint64_t t0)
{
    struct hstate *s = &hs[h];
    uint32_t w = width_of[h];
    if (t0 > fstReaderGetStartTime(ctx)) {
        /* past the end the value is the last one */
        uint64_t t = t0 - 1, end = fstReaderGetEndTime(ctx);
        char *v = fstReaderGetValueFromHandleAtTime(ctx, t > end ? end : t, h, valbuf);
        if (v && strlen(v) >= w)
            memcpy(s->last, v, w);
        else
            memset(s->last, 'x', w);
    } else {
        memset(s->last, 'x', w);
    }
}

static int parse_handles(char **tok, int n, fstHandle *out)
{
    int k = 0;
    for (int i = 0; i < n; i++) {
        unsigned long h = strtoul(tok[i], NULL, 10);
        if (h == 0 || h > max_handle || width_of[h] == 0)
            continue;
        out[k++] = (fstHandle)h;
    }
    return k;
}

static void run_query(fstHandle *hl, int nh)
{
    fstReaderClrFacProcessMaskAll(ctx);
    for (int i = 0; i < nh; i++) {
        fstHandle h = hl[i];
        struct hstate *s = &hs[h];
        if (!s->last) {
            s->last = malloc(width_of[h] + 1);
            s->bucket_last = malloc(width_of[h] + 1);
        }
        s->active = 1;
        s->bucket = -1;
        s->changes = s->to_one = 0;
        prior(h, q_t0);
        printf("p %u ", h);
        out_value(s->last, width_of[h]);
        putchar('\n');
        fstReaderSetFacProcessMask(ctx, h);
    }
    fstReaderSetLimitTimeRange(ctx, q_t0, q_t1);
    fstReaderIterBlocks2(ctx, on_change, on_change_varlen, NULL, NULL);
    for (int i = 0; i < nh; i++) {
        if (mode_agg)
            flush_bucket(hl[i]);
        hs[hl[i]].active = 0;
    }
    printf(".\n");
}

/* --scope A.B.C: list only the variables below that scope, or that one
 * variable; several may be given */
#define MAX_SCOPES 64
static const char *want_scope[MAX_SCOPES];
static int n_want;

static int in_scope(const char *path, int match_depth, const char *leaf)
{
    if (!n_want || match_depth >= 0)
        return 1;
    size_t pl = strlen(path);
    for (int i = 0; i < n_want; i++) {
        const char *w = want_scope[i];
        if (pl == 0) {
            if (!strcmp(w, leaf))
                return 1;
        } else if (!strncmp(w, path, pl) && w[pl] == '.' && !strcmp(w + pl + 1, leaf)) {
            return 1;
        }
    }
    return 0;
}

/* the declared type and port direction, for the "vk" line after each var */
static const char *var_type_name(unsigned char typ)
{
    switch (typ) {
    case FST_VT_VCD_EVENT: return "event";
    case FST_VT_VCD_INTEGER: return "integer";
    case FST_VT_VCD_PARAMETER: return "parameter";
    case FST_VT_VCD_REG: return "reg";
    case FST_VT_VCD_SUPPLY0: return "supply0";
    case FST_VT_VCD_SUPPLY1: return "supply1";
    case FST_VT_VCD_TIME: return "time";
    case FST_VT_VCD_TRI: return "tri";
    case FST_VT_VCD_TRIAND: return "triand";
    case FST_VT_VCD_TRIOR: return "trior";
    case FST_VT_VCD_TRIREG: return "trireg";
    case FST_VT_VCD_TRI0: return "tri0";
    case FST_VT_VCD_TRI1: return "tri1";
    case FST_VT_VCD_WAND: return "wand";
    case FST_VT_VCD_WIRE: return "wire";
    case FST_VT_VCD_WOR: return "wor";
    case FST_VT_VCD_PORT: return "port";
    case FST_VT_SV_BIT: return "bit";
    case FST_VT_SV_LOGIC: return "logic";
    case FST_VT_SV_INT: return "int";
    case FST_VT_SV_SHORTINT: return "shortint";
    case FST_VT_SV_LONGINT: return "longint";
    case FST_VT_SV_BYTE: return "byte";
    case FST_VT_SV_ENUM: return "enum";
    default: return "-";
    }
}

static const char *var_dir_name(unsigned char dir)
{
    switch (dir) {
    case FST_VD_INPUT: return "in";
    case FST_VD_OUTPUT: return "out";
    case FST_VD_INOUT: return "inout";
    case FST_VD_BUFFER: return "buffer";
    default: return "-";
    }
}

static void hierarchy(void)
{
    struct fstHier *hier;
    char path[MAX_LINE];
    size_t depth_len[1024];
    int depth = 0;
    int match_depth = -1; /* depth of the scope --scope named, -1 outside */
    path[0] = 0;
    fstReaderIterateHierRewind(ctx);
    while ((hier = fstReaderIterateHier(ctx))) {
        switch (hier->htyp) {
        case FST_HT_SCOPE: {
            size_t len = strlen(path);
            if (depth < 1024)
                depth_len[depth] = len;
            depth++;
            if (len)
                strncat(path, ".", sizeof(path) - strlen(path) - 1);
            strncat(path, hier->u.scope.name, sizeof(path) - strlen(path) - 1);
            if (n_want && match_depth < 0)
                for (int i = 0; i < n_want; i++)
                    if (!strcmp(want_scope[i], path)) {
                        match_depth = depth;
                        break;
                    }
            break;
        }
        case FST_HT_UPSCOPE:
            if (depth > 0) {
                depth--;
                if (depth < 1024)
                    path[depth_len[depth]] = 0;
                if (match_depth > depth)
                    match_depth = -1;
            }
            break;
        case FST_HT_VAR: {
            unsigned char typ = hier->u.var.typ;
            if (typ == FST_VT_VCD_REAL || typ == FST_VT_VCD_REAL_PARAMETER || typ == FST_VT_VCD_REALTIME ||
                typ == FST_VT_SV_SHORTREAL || typ == FST_VT_GEN_STRING)
                break;
            fstHandle h = hier->u.var.handle;
            /* the name may carry a packed range: "data [7:0]" */
            char name[4096];
            snprintf(name, sizeof(name), "%s", hier->u.var.name);
            char *sp = strchr(name, ' ');
            if (sp)
                *sp = 0;
            if (!in_scope(path, match_depth, name))
                break;
            if (h <= max_handle)
                width_of[h] = hier->u.var.length;
            /* the declared type and direction follow the name after a tab: one line per var */
            printf("var %u %u %s%s%s\t%s %s\n", h, hier->u.var.length, path, path[0] ? "." : "", name,
                   var_type_name(typ), var_dir_name(hier->u.var.direction));
            break;
        }
        default:
            break;
        }
    }
}

int main(int argc, char **argv)
{
    int bad = argc < 2;
    for (int i = 2; i < argc && !bad; i++) {
        if (!strcmp(argv[i], "--scope") && i + 1 < argc && n_want < MAX_SCOPES)
            want_scope[n_want++] = argv[++i];
        else
            bad = 1;
    }
    if (bad) {
        fprintf(stderr, "usage: fstsrv <dump.fst> [--scope A.B.C]...\n");
        return 2;
    }
    ctx = fstReaderOpen(argv[1]);
    if (!ctx) {
        printf("! cannot open %s as FST\n", argv[1]);
        return 1;
    }
    max_handle = fstReaderGetMaxHandle(ctx);
    width_of = calloc(max_handle + 1, sizeof(*width_of));
    hs = calloc(max_handle + 1, sizeof(*hs));
    valbuf = malloc(1 << 20);
    printf("wavesrv 1\n");
    printf("timescale %d\n", (int)fstReaderGetTimescale(ctx));
    printf("range %" PRIu64 " %" PRIu64 "\n", fstReaderGetStartTime(ctx), fstReaderGetEndTime(ctx));
    hierarchy();
    printf("end\n");
    fflush(stdout);

    char *line = malloc(MAX_LINE);
    char **tok = malloc(sizeof(char *) * (MAX_LINE / 2));
    fstHandle *hl = malloc(sizeof(fstHandle) * (MAX_LINE / 2));
    while (fgets(line, MAX_LINE, stdin)) {
        int n = 0;
        for (char *p = strtok(line, " \t\r\n"); p; p = strtok(NULL, " \t\r\n"))
            tok[n++] = p;
        if (n == 0)
            continue;
        if (!strcmp(tok[0], "quit"))
            break;
        if (!strcmp(tok[0], "read") && n >= 4) {
            mode_agg = 0;
            q_t0 = strtoull(tok[1], NULL, 10);
            q_t1 = strtoull(tok[2], NULL, 10);
            int nh = parse_handles(tok + 3, n - 3, hl);
            run_query(hl, nh);
        } else if (!strcmp(tok[0], "agg") && n >= 5) {
            mode_agg = 1;
            q_t0 = strtoull(tok[1], NULL, 10);
            q_level = atoi(tok[2]);
            uint64_t nb = strtoull(tok[3], NULL, 10);
            q_t1 = q_t0 + (nb << q_level) - 1;
            int nh = parse_handles(tok + 4, n - 4, hl);
            run_query(hl, nh);
        } else if (!strcmp(tok[0], "stat")) {
            struct stat st;
            if (stat(argv[1], &st) == 0)
                printf("s %lld %lld\n.\n", (long long)st.st_size, (long long)st.st_mtime);
            else
                printf("! cannot stat %s\n", argv[1]);
        } else if (!strcmp(tok[0], "at") && n >= 3) {
            uint64_t t = strtoull(tok[1], NULL, 10);
            if (t > fstReaderGetEndTime(ctx))
                t = fstReaderGetEndTime(ctx);
            int nh = parse_handles(tok + 2, n - 2, hl);
            for (int i = 0; i < nh; i++) {
                char *v = fstReaderGetValueFromHandleAtTime(ctx, t, hl[i], valbuf);
                printf("v %u %s\n", hl[i], v ? v : "x");
            }
            printf(".\n");
        } else {
            printf("! unknown command %s\n", tok[0]);
        }
        fflush(stdout);
    }
    fstReaderClose(ctx);
    return 0;
}
