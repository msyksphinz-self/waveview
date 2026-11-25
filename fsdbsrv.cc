/* fsdbsrv: serve an FSDB dump on stdin/stdout without converting it.
 *
 * The FSDB twin of fstsrv.c: the same line protocol (see there), so
 * wavesource.py drives either one and the viewer does not know the
 * difference. FSDB is Synopsys' closed format; this reads it through the
 * FsdbReader library that ships with Verdi ($VERDI_HOME/share/FsdbReader:
 * ffrAPI.h, libnffr, libnsys), so it is built where Verdi is, usually the
 * build server, and runs there (over ssh from a desktop, like fstsrv).
 *
 * Build (wave2img.build_fsdb_tool and remote.py do this on demand):
 *   c++ -O2 -w -I$VERDI_HOME/share/FsdbReader -o fsdbsrv fsdbsrv.cc \
 *       -L$VERDI_HOME/share/FsdbReader/LINUX64 -Wl,-rpath,$VERDI_HOME/share/FsdbReader/LINUX64 \
 *       -lnffr -lnsys -ldl -lpthread -lz
 *
 * How it reads: a query loads just the requested variables (the library
 * reads their value-change chains from the file), walks each from the last
 * change before t0 to the first after t1, and unloads them. Opening reads
 * only the scope/variable tree. Real, string and transaction variables are
 * not served; a variable whose values are not one byte per bit shows as x.
 *
 * Options, after the dump:
 *   --scope A.B.C   list only the variables below that scope (or that one
 *                   variable); may be given several times
 *   --list-max N    list at most N variables (with --scope, usually): past
 *                   that the rest are counted, and the header ends with the
 *                   "index <variables> <scopes>" and "scope" summary lines
 *                   below instead, so the client can ask for a smaller scope
 *   --index FILE --index-min N
 *                   a dump with N variable idcodes or more is not listed in
 *                   the protocol: its names go to FILE (the format is below)
 *                   and the header says "index <variables> <scopes>"
 *                   instead, with "scope <variables> <path>" and
 *                   "clock <id> <scope number> <name>" lines for the
 *                   summary. wavesource.py keeps the file and looks names up
 *                   in it (nameindex.py)
 *   --no-tree N     the client has that index, of a dump with N scopes: only
 *                   the scopes are read (seconds where the variables take
 *                   minutes to list), and before values are asked for the
 *                   client names the scopes of the variables:
 *                       vars <scope number> [<scope number>...]
 *                       .
 *                   The library cannot decode a variable it has not read
 *                   the declaration of, and reads those a scope at a time.
 *                   Scopes are numbered from 1 in the order of the tree: the
 *                   place the library reads a scope's variables from is not
 *                   a file offset in every FSDB, so it cannot be kept
 *
 * The names file is the scope tree in the order it is stored:
 *   +name           enter a scope (the n-th such line is scope number n)
 *   *name           enter a struct: part of a name, not a scope of its own
 *   -               leave either
 *   id width name   a variable
 *   @n<TAB>c<TAB>a<TAB>b<TAB>*s
 *                   where the next line is: in scope number n (0: none), c
 *                   scopes entered so far, and the names in full. Written
 *                   after a "-" before a variable or a struct, and about
 *                   every 16 KB, so that the place of any line is found by
 *                   reading back a little
 *
 * The FsdbReader library writes log lines to stdout and a banner to stderr.
 * The protocol therefore goes to a duplicate of the original stdout, and
 * file descriptors 1 and 2 are pointed at a scratch file before the library
 * is touched (FSDBSRV_VERBOSE=1 leaves stderr alone).
 */

#ifdef NOVAS_FSDB
#undef NOVAS_FSDB
#endif

#include "ffrAPI.h"

#include <ctype.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* ffrAPI.h uses bool_T but leaves TRUE / FALSE to the application */
#ifndef FALSE
#define FALSE 0
#endif
#ifndef TRUE
#define TRUE 1
#endif

#define MAX_LINE (1 << 20)
#define MAX_DEPTH 1024
#define T2U64(t) (((uint64_t)(t).H << 32) | (uint64_t)(t).L)
#define XT2U64(xt) (((uint64_t)(xt).t64.H << 32) | (uint64_t)(xt).t64.L)

static FILE *out;          /* the protocol; fd 1 itself takes the library's chatter */
static ffrObject *fsdb;
static fsdbVarIdcode max_id;
static uint32_t *width_of; /* per idcode, 0 = not served */
static uint64_t t_min, t_max;

/* per requested variable state */
struct hstate {
    char *last;        /* last emitted value */
    /* aggregation */
    int64_t bucket;
    uint64_t first_t, last_t;
    uint64_t changes, to_one;
    char *bucket_last;
};
static struct hstate *hs;

/* options */
#define MAX_SCOPES 64
static const char *want_scope[MAX_SCOPES];
static int n_want;
static int no_tree;
static uint64_t expect_scopes;
static const char *index_path;
static uint64_t index_min;
static uint64_t list_max;    /* --list-max: past this many listed variables, count and summarise only */

static int mode_agg;
static uint64_t q_t0, q_t1;
static int q_level;

/* -- hierarchy ------------------------------------------------------------ */

static char path[MAX_LINE];
static size_t depth_len[MAX_DEPTH];
static int depth;

static int match_depth = -1; /* depth of the scope --scope named, -1 outside */
static int declaring;        /* reading the variables of one scope for "vars" */

/* --no-tree: where the library reads the variables of each scope from */
static fsdbLUOff *scope_off;
static char *scope_read;
static uint64_t n_scope_off, cap_scope_off;

static bool_T scopes_cb(fsdbTreeCBType cb_type, void *client_data, void *tree_cb_data)
{
    (void)client_data;
    if (cb_type != FSDB_TREE_CBT_SCOPE)
        return TRUE;
    if (n_scope_off + 1 >= cap_scope_off) {
        cap_scope_off = cap_scope_off ? cap_scope_off * 2 : 4096;
        scope_off = (fsdbLUOff *)realloc(scope_off, cap_scope_off * sizeof(*scope_off));
        if (!scope_off) {
            fprintf(out, "! out of memory for the scopes\n");
            fflush(out);
            exit(1);
        }
    }
    scope_off[++n_scope_off] = ((fsdbTreeCBDataScope *)tree_cb_data)->var_start_log_uoff;
    return TRUE;
}
static uint64_t n_listed;

/* -- the names file (--index) ---------------------------------------------- */

#define CHECKPOINT (16 * 1024)
#define MAX_CLOCKS 16
static FILE *idx;
static long idx_since;     /* bytes since the last "@" line */
static int idx_after_pop;
/* the scopes file next to the names file: "<start> <end> <variables> <path>"
 * for every scope holding BIG_SCOPE variables or more, so that the client
 * finds a large scope and knows its size without reading through it */
#define BIG_SCOPE 1000
static FILE *idx_scopes;
static uint64_t scope_start[MAX_DEPTH + 1]; /* offset of the "+" line of the scope at each depth */
static uint64_t scope_vars[MAX_DEPTH + 1];  /* variables below the "+" scope at each depth so far */
static int plus_at[MAX_DEPTH + 1];          /* depth of the innermost "+" scope at each depth */
static uint64_t n_scopes;                /* "+" lines so far */
static uint64_t ord_at[MAX_DEPTH + 1];   /* the scope number at each depth */
static char kind_at[MAX_DEPTH + 1];      /* '+' or '*' of the name at each depth */
struct bucket {
    char *key;
    uint64_t n;
};
static struct bucket *buckets; /* variables per scope of the first SUMMARY_DEPTH levels */
static size_t n_buckets, cap_buckets;
#define SUMMARY_DEPTH 6 /* scopes this deep get a count of their own in the summary */
static size_t bucket_at[SUMMARY_DEPTH + 1];
struct clock_cand {
    fsdbVarIdcode id;
    uint64_t ord;
    size_t len;
    char *name;
};
static struct clock_cand clocks[MAX_CLOCKS];
static int n_clocks;

static size_t new_bucket(const char *key)
{
    if (n_buckets == cap_buckets) {
        cap_buckets = cap_buckets ? cap_buckets * 2 : 256;
        buckets = (struct bucket *)realloc(buckets, cap_buckets * sizeof(*buckets));
        if (!buckets) {
            fprintf(out, "! out of memory\n");
            fflush(out);
            exit(1);
        }
    }
    buckets[n_buckets].key = strdup(key);
    buckets[n_buckets].n = 0;
    return n_buckets++;
}

/* "@" and the current scope, its names separated by tabs */
static void idx_checkpoint(void)
{
    int d = depth < MAX_DEPTH ? depth : MAX_DEPTH;
    fprintf(idx, "@%" PRIu64 "\t%" PRIu64, ord_at[d], n_scopes);
    for (int i = 0; i < d; i++) {
        size_t a = depth_len[i] + (i ? 1 : 0);
        size_t b = i + 1 < d ? depth_len[i + 1] : strlen(path);
        fputc('\t', idx);
        if (kind_at[i + 1] == '*')
            fputc('*', idx);
        fwrite(path + a, 1, b > a ? b - a : 0, idx);
    }
    fputc('\n', idx);
    idx_since = 0;
    idx_after_pop = 0;
}

static int has_clock_name(const char *name)
{
    char low[64];
    size_t n = strlen(name);
    if (n >= sizeof(low))
        n = sizeof(low) - 1;
    for (size_t i = 0; i < n; i++)
        low[i] = (char)tolower((unsigned char)name[i]);
    low[n] = 0;
    return strstr(low, "clk") || strstr(low, "clock");
}

/* keep the MAX_CLOCKS clock-like variables with the shortest names */
static void note_clock(fsdbVarIdcode id, const char *name)
{
    size_t len = strlen(path) + 1 + strlen(name);
    int at = -1;
    for (int i = 0; i < n_clocks; i++)
        if (clocks[i].id == id) {
            if (clocks[i].len <= len)
                return;
            at = i;
        }
    if (at < 0) {
        if (n_clocks < MAX_CLOCKS) {
            at = n_clocks++;
            clocks[at].name = NULL;
        } else {
            at = 0;
            for (int i = 1; i < n_clocks; i++)
                if (clocks[i].len > clocks[at].len)
                    at = i;
            if (clocks[at].len <= len)
                return;
        }
    }
    free(clocks[at].name);
    clocks[at].name = (char *)malloc(len + 1);
    if (!clocks[at].name) {
        clocks[at].len = (size_t)-1;
        clocks[at].id = 0;
        return;
    }
    snprintf(clocks[at].name, len + 1, "%s%s%s", path, path[0] ? "." : "", name);
    clocks[at].id = id;
    clocks[at].ord = ord_at[depth < MAX_DEPTH ? depth : MAX_DEPTH];
    clocks[at].len = len;
}

/* -- the scope tree -------------------------------------------------------- */

static int in_scope(const char *leaf)
{
    if (!n_want || match_depth >= 0)
        return 1;
    if (!leaf)
        return 0;
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

static void push_scope(const char *name, char kind)
{
    if (idx) {
        if (idx_since >= CHECKPOINT || (idx_after_pop && kind == '*'))
            idx_checkpoint();
        if (depth < MAX_DEPTH) {
            scope_start[depth + 1] = (uint64_t)ftello(idx);
            scope_vars[depth + 1] = 0;
            plus_at[depth + 1] = kind == '+' ? depth + 1 : plus_at[depth];
        }
        idx_since += fprintf(idx, "%c%s\n", kind, name ? name : "");
        idx_after_pop = 0;
        if (kind == '+')
            n_scopes++;
        if (depth < MAX_DEPTH) {
            ord_at[depth + 1] = kind == '+' ? n_scopes : ord_at[depth];
            kind_at[depth + 1] = kind;
        }
    }
    size_t len = strlen(path);
    if (depth < MAX_DEPTH)
        depth_len[depth] = len;
    depth++;
    if (len)
        strncat(path, ".", sizeof(path) - strlen(path) - 1);
    strncat(path, name ? name : "", sizeof(path) - strlen(path) - 1);
    if (n_want && match_depth < 0)
        for (int i = 0; i < n_want; i++)
            if (!strcmp(want_scope[i], path)) {
                match_depth = depth;
                break;
            }
    if ((idx || list_max) && depth <= SUMMARY_DEPTH)
        bucket_at[depth] = new_bucket(path);
}

static void pop_scope(void)
{
    if (depth > 0) {
        if (idx) {
            idx_since += fprintf(idx, "-\n");
            idx_after_pop = 1;
            if (depth <= MAX_DEPTH && kind_at[depth] == '+') {
                uint64_t n = scope_vars[depth];
                if (n >= BIG_SCOPE && idx_scopes)
                    fprintf(idx_scopes, "%" PRIu64 " %" PRIu64 " %" PRIu64 " %s\n", scope_start[depth],
                            (uint64_t)ftello(idx), n, path);
                if (depth > 1)
                    scope_vars[plus_at[depth - 1]] += n;
            }
        }
        depth--;
        if (depth < MAX_DEPTH)
            path[depth_len[depth]] = 0;
        if (match_depth > depth)
            match_depth = -1;
    }
}

/* -- the declared type and port direction, for the "vk" line after a var -- */

static const char *var_type_name(int t)
{
    switch (t) {
    case FSDB_VT_VCD_EVENT: return "event";
    case FSDB_VT_VCD_INTEGER: return "integer";
    case FSDB_VT_VCD_PARAMETER: return "parameter";
    case FSDB_VT_VCD_REG: return "reg";
    case FSDB_VT_VCD_SUPPLY0: return "supply0";
    case FSDB_VT_VCD_SUPPLY1: return "supply1";
    case FSDB_VT_VCD_TIME: return "time";
    case FSDB_VT_VCD_TRI: return "tri";
    case FSDB_VT_VCD_TRIAND: return "triand";
    case FSDB_VT_VCD_TRIOR: return "trior";
    case FSDB_VT_VCD_TRIREG: return "trireg";
    case FSDB_VT_VCD_TRI0: return "tri0";
    case FSDB_VT_VCD_TRI1: return "tri1";
    case FSDB_VT_VCD_WAND: return "wand";
    case FSDB_VT_VCD_WIRE: return "wire";
    case FSDB_VT_VCD_WOR: return "wor";
    case FSDB_VT_VCD_PORT: return "port";
    case FSDB_VT_VCD_MEMORY: return "memory";
    case FSDB_VT_VHDL_SIGNAL: return "signal";
    case FSDB_VT_VHDL_VARIABLE: return "variable";
    case FSDB_VT_VHDL_CONSTANT: return "constant";
    default: return "-";
    }
}

/* fsdbTreeCBDataVar carries the port direction in some FsdbReader versions
 * (direction: FSDB_VD_IMPLICIT 0, INPUT 1, OUTPUT 2, INOUT 3, like FST's).
 * Looked up only where the member exists, so this builds either way. */
template <typename T>
static auto var_direction(const T *v, int) -> decltype((int)v->direction)
{
    return (int)v->direction;
}
template <typename T>
static int var_direction(const T *, long)
{
    return 0;
}

static const char *var_dir_name(int d)
{
    switch (d) {
    case 1: return "in";
    case 2: return "out";
    case 3: return "inout";
    default: return "-";
    }
}

static void emit_var(fsdbTreeCBDataVar *var)
{
    switch (var->type) {
    case FSDB_VT_VCD_REAL:
    case FSDB_VT_STREAM:
        return;
    default:
        break;
    }
    if (var->vc_dt == FSDB_VC_DT_FLOAT || var->vc_dt == FSDB_VC_DT_DOUBLE)
        return;
    fsdbVarIdcode id = var->u.idcode;
    if (id <= 0 || id > max_id)
        return;
    /* the name may carry the packed range: "data[7:0]"; a word of a memory,
     * "mem[3]", has no ':' and stays */
    char name[4096];
    snprintf(name, sizeof(name), "%s", var->name ? var->name : "");
    size_t n = strlen(name);
    if (n && name[n - 1] == ']') {
        char *lb = strrchr(name, '[');
        if (lb && lb != name && strchr(lb, ':'))
            *lb = 0;
    }
    if (declaring) { /* "vars": the library has read the declaration, that is all */
        width_of[id] = (uint32_t)(var->lbitnum >= var->rbitnum ? var->lbitnum - var->rbitnum : var->rbitnum - var->lbitnum) + 1;
        return;
    }
    if (!in_scope(name))
        return;
    uint32_t w = (uint32_t)(var->lbitnum >= var->rbitnum ? var->lbitnum - var->rbitnum : var->rbitnum - var->lbitnum) + 1;
    width_of[id] = w;
    n_listed++;
    if (!idx) {
        /* the declared type and direction follow the name after a tab: one line per var */
        if (!list_max || n_listed <= list_max)
            fprintf(out, "var %u %u %s%s%s\t%s %s\n", (unsigned)id, w, path, path[0] ? "." : "", name,
                    var_type_name(var->type), var_dir_name(var_direction(var, 0)));
        if (list_max) /* past the cap the listing is a summary: count per scope */
            buckets[bucket_at[depth < SUMMARY_DEPTH ? depth : SUMMARY_DEPTH]].n++;
        return;
    }
    if (idx_after_pop || idx_since >= CHECKPOINT)
        idx_checkpoint();
    idx_since += fprintf(idx, "%u %u %s\n", (unsigned)id, w, name);
    if (depth <= MAX_DEPTH)
        scope_vars[plus_at[depth]]++;
    buckets[bucket_at[depth < SUMMARY_DEPTH ? depth : SUMMARY_DEPTH]].n++;
    if (w == 1 && has_clock_name(name))
        note_clock(id, name);
}

static bool_T tree_cb(fsdbTreeCBType cb_type, void *client_data, void *tree_cb_data)
{
    (void)client_data;
    switch (cb_type) {
    case FSDB_TREE_CBT_SCOPE:
        push_scope(((fsdbTreeCBDataScope *)tree_cb_data)->name, '+');
        break;
    case FSDB_TREE_CBT_STRUCT_BEGIN:
        push_scope(((fsdbTreeCBDataStructBegin *)tree_cb_data)->name, '*');
        break;
    case FSDB_TREE_CBT_UPSCOPE:
    case FSDB_TREE_CBT_STRUCT_END:
        pop_scope();
        break;
    case FSDB_TREE_CBT_VAR:
        emit_var((fsdbTreeCBDataVar *)tree_cb_data);
        break;
    default:
        break;
    }
    return TRUE;
}

static bool_T quiet_cb(fsdbTreeCBType cb_type, void *client_data, void *tree_cb_data)
{
    (void)cb_type;
    (void)client_data;
    (void)tree_cb_data;
    return TRUE;
}

static int timescale_exp(void)
{
    uint_T digit = 1;
    char *unit = NULL;
    str_T su = fsdb->ffrGetScaleUnit();
    if (fsdb->ffrExtractScaleUnit(su, digit, unit) != FSDB_RC_SUCCESS || !unit)
        return -9;
    int e;
    switch (tolower((unsigned char)unit[0])) {
    case 'm': e = -3; break;
    case 'u': e = -6; break;
    case 'n': e = -9; break;
    case 'p': e = -12; break;
    case 'f': e = -15; break;
    case 'a': e = -18; break;
    case 'z': e = -21; break;
    default: e = 0; break;
    }
    for (uint_T d = digit ? digit : 1; d >= 10; d /= 10)
        e++;
    return e;
}

/* -- values --------------------------------------------------------------- */

static void out_value(const char *v, uint32_t w)
{
    fwrite(v, 1, w, out);
}

static uint64_t cur_time(ffrVCTrvsHdl hdl)
{
    ffrXTag xt;
    if (hdl->ffrGetXTag(&xt) != FSDB_RC_SUCCESS)
        return t_max + 1;
    return XT2U64(xt);
}

/* the current value change of hdl as w characters of 0/1/x/z, right aligned */
static void decode(ffrVCTrvsHdl hdl, char *buf, uint32_t w)
{
    byte_T *vc = NULL;
    if (hdl->ffrGetVC(&vc) != FSDB_RC_SUCCESS || !vc || hdl->ffrGetBytesPerBit() != FSDB_BYTES_PER_BIT_1B) {
        memset(buf, 'x', w);
        return;
    }
    uint32_t bs = (uint32_t)hdl->ffrGetBitSize();
    uint32_t pad = bs < w ? w - bs : 0;
    uint32_t skip = bs > w ? bs - w : 0;
    memset(buf, '0', pad);
    for (uint32_t i = 0; i + pad < w; i++) {
        char c;
        switch (vc[skip + i]) {
        case FSDB_BT_VCD_0: c = '0'; break;
        case FSDB_BT_VCD_1: c = '1'; break;
        case FSDB_BT_VCD_Z: c = 'z'; break;
        default: c = 'x'; break;
        }
        buf[pad + i] = c;
    }
}

static void flush_bucket(fsdbVarIdcode h)
{
    struct hstate *s = &hs[h];
    if (s->bucket < 0 || s->changes == 0)
        return;
    fprintf(out, "b %u %" PRId64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " ", (unsigned)h, s->bucket,
            s->first_t, s->last_t, s->changes, s->to_one);
    out_value(s->bucket_last, width_of[h]);
    fputc('\n', out);
    s->changes = 0;
    s->to_one = 0;
}

static void on_change(uint64_t t, fsdbVarIdcode h, const char *value)
{
    struct hstate *s = &hs[h];
    uint32_t w = width_of[h];
    if (memcmp(s->last, value, w) == 0)
        return; /* rewrite of the same value */
    memcpy(s->last, value, w);
    if (!mode_agg) {
        fprintf(out, "c %" PRIu64 " %u ", t, (unsigned)h);
        out_value(value, w);
        fputc('\n', out);
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

static char *valbuf;

/* Position hdl at the first value change at or after t0, folding the
 * changes before it into s->last (the value held at t0). Returns whether
 * hdl is on a change. ffrGotoXTag lands on the change at or just before the
 * time; the loops make this right whichever side it lands on. */
static int seek(ffrVCTrvsHdl hdl, struct hstate *s, uint32_t w, uint64_t t0)
{
    int have = 0;
    if (hdl && hdl->ffrHasIncoreVC() == TRUE) {
        fsdbTag64 tag;
        tag.H = (uint32_t)(t0 >> 32);
        tag.L = (uint32_t)(t0 & 0xFFFFFFFFu);
        if (hdl->ffrGotoXTag((void *)&tag) == FSDB_RC_SUCCESS) {
            have = 1;
            while (cur_time(hdl) >= t0 && hdl->ffrGotoPrevVC() == FSDB_RC_SUCCESS)
                ;
        } else if (hdl->ffrGotoTheFirstVC() == FSDB_RC_SUCCESS) {
            have = 1;
        }
    }
    while (have && cur_time(hdl) < t0) {
        decode(hdl, s->last, w);
        have = hdl->ffrGotoNextVC() == FSDB_RC_SUCCESS;
    }
    return have;
}

static void serve_one(fsdbVarIdcode h)
{
    struct hstate *s = &hs[h];
    uint32_t w = width_of[h];
    if (!s->last) {
        s->last = (char *)malloc(w + 1);
        s->bucket_last = (char *)malloc(w + 1);
    }
    memset(s->last, 'x', w);
    s->bucket = -1;
    s->changes = s->to_one = 0;
    ffrVCTrvsHdl hdl = fsdb->ffrCreateVCTraverseHandle(h);
    int have = seek(hdl, s, w, q_t0);
    fprintf(out, "p %u ", (unsigned)h);
    out_value(s->last, w);
    fputc('\n', out);
    while (have) {
        uint64_t t = cur_time(hdl);
        if (t > q_t1)
            break;
        decode(hdl, valbuf, w);
        on_change(t, h, valbuf);
        have = hdl->ffrGotoNextVC() == FSDB_RC_SUCCESS;
    }
    if (mode_agg)
        flush_bucket(h);
    if (hdl)
        hdl->ffrFree();
}

static int parse_handles(char **tok, int n, fsdbVarIdcode *hl)
{
    int k = 0;
    for (int i = 0; i < n; i++) {
        unsigned long h = strtoul(tok[i], NULL, 10);
        if (h == 0 || h > (unsigned long)max_id || width_of[h] == 0)
            continue;
        hl[k++] = (fsdbVarIdcode)h;
    }
    return k;
}

static int load(fsdbVarIdcode *hl, int nh)
{
    fsdb->ffrResetSignalList();
    if (nh == 0)
        return 1;
    for (int i = 0; i < nh; i++)
        fsdb->ffrAddToSignalList(hl[i]);
    if (fsdb->ffrLoadSignals() != FSDB_RC_SUCCESS) {
        fsdb->ffrResetSignalList();
        fprintf(out, "! the FSDB reader could not load the value changes\n");
        return 0;
    }
    return 1;
}

static void unload(void)
{
    fsdb->ffrUnloadSignals();
    fsdb->ffrResetSignalList();
}

static void run_query(fsdbVarIdcode *hl, int nh)
{
    if (!load(hl, nh))
        return;
    for (int i = 0; i < nh; i++)
        serve_one(hl[i]);
    unload();
    fprintf(out, ".\n");
}

static void run_at(uint64_t t, fsdbVarIdcode *hl, int nh)
{
    if (!load(hl, nh))
        return;
    for (int i = 0; i < nh; i++) {
        fsdbVarIdcode h = hl[i];
        struct hstate *s = &hs[h];
        uint32_t w = width_of[h];
        if (!s->last) {
            s->last = (char *)malloc(w + 1);
            s->bucket_last = (char *)malloc(w + 1);
        }
        memset(s->last, 'x', w);
        ffrVCTrvsHdl hdl = fsdb->ffrCreateVCTraverseHandle(h);
        seek(hdl, s, w, t + 1); /* the value after the changes at t */
        if (hdl)
            hdl->ffrFree();
        fprintf(out, "v %u ", (unsigned)h);
        out_value(s->last, w);
        fputc('\n', out);
    }
    unload();
    fprintf(out, ".\n");
}

int main(int argc, char **argv)
{
    int bad = argc < 2;
    for (int i = 2; i < argc && !bad; i++) {
        if (!strcmp(argv[i], "--scope") && i + 1 < argc && n_want < MAX_SCOPES)
            want_scope[n_want++] = argv[++i];
        else if (!strcmp(argv[i], "--index") && i + 1 < argc)
            index_path = argv[++i];
        else if (!strcmp(argv[i], "--index-min") && i + 1 < argc)
            index_min = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--list-max") && i + 1 < argc)
            list_max = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--no-tree") && i + 1 < argc) {
            no_tree = 1;
            expect_scopes = strtoull(argv[++i], NULL, 10);
        }
        else
            bad = 1;
    }
    if (bad) {
        fprintf(stderr, "usage: fsdbsrv <dump.fsdb> [--scope A.B.C]... [--list-max N] [--index FILE --index-min N] [--no-tree N]\n");
        return 2;
    }
    /* keep the protocol away from the library's log lines */
    int proto_fd = dup(1);
    FILE *scratch = tmpfile();
    if (proto_fd < 0 || !scratch) {
        fprintf(stderr, "fsdbsrv: cannot set up stdout\n");
        return 2;
    }
    dup2(fileno(scratch), 1);
    /* the banner and *WARN* lines go to stderr: the same scratch file, as
     * every failure is reported in the protocol. FSDBSRV_VERBOSE keeps them */
    if (!getenv("FSDBSRV_VERBOSE"))
        dup2(fileno(scratch), 2);
    out = fdopen(proto_fd, "w");
    setvbuf(out, NULL, _IOFBF, 1 << 16);

    if (!ffrObject::ffrIsFSDB(argv[1])) {
        fprintf(out, "! %s is not an FSDB file\n", argv[1]);
        fflush(out);
        return 1;
    }
    fsdb = ffrObject::ffrOpen3(argv[1]);
    if (!fsdb) {
        fprintf(out, "! cannot open %s as FSDB\n", argv[1]);
        fflush(out);
        return 1;
    }
    fsdb->ffrSetTreeCBFunc(quiet_cb, NULL);
    fsdbFileType ft = fsdb->ffrGetFileType();
    if (ft != FSDB_FT_VERILOG && ft != FSDB_FT_VERILOG_VHDL && ft != FSDB_FT_VHDL) {
        fprintf(out, "! %s holds no signal dump (file type %d)\n", argv[1], (int)ft);
        fflush(out);
        return 1;
    }
    uint_T blk_idx = 0;
    fsdb->ffrReadDataTypeDefByBlkIdx(blk_idx); /* keeps transaction data from tripping the reader */

    fsdbTag64 tag;
    t_min = fsdb->ffrGetMinFsdbTag64(&tag) == FSDB_RC_SUCCESS ? T2U64(tag) : 0;
    t_max = fsdb->ffrGetMaxFsdbTag64(&tag) == FSDB_RC_SUCCESS ? T2U64(tag) : t_min;

    max_id = fsdb->ffrGetMaxVarIdcode();
    width_of = (uint32_t *)calloc((size_t)max_id + 1, sizeof(*width_of));
    hs = (struct hstate *)calloc((size_t)max_id + 1, sizeof(*hs));
    valbuf = (char *)malloc(1 << 20);
    char *line = (char *)malloc(MAX_LINE);
    char **tok = (char **)malloc(sizeof(char *) * (MAX_LINE / 2));
    fsdbVarIdcode *hl = (fsdbVarIdcode *)malloc(sizeof(fsdbVarIdcode) * (MAX_LINE / 2));
    if (!width_of || !hs || !valbuf || !line || !tok || !hl) {
        fprintf(out, "! out of memory for %s (%ld variables)\n", argv[1], (long)max_id);
        fflush(out);
        return 1;
    }

    fprintf(out, "wavesrv 1\n");
    fprintf(out, "timescale %d\n", timescale_exp());
    fprintf(out, "range %" PRIu64 " %" PRIu64 "\n", t_min, t_max);
    if (index_path && !n_want && !no_tree && (uint64_t)max_id >= index_min) {
        idx = fopen(index_path, "w");
        if (!idx) {
            fprintf(out, "! cannot write the name index %s\n", index_path);
            fflush(out);
            return 1;
        }
        setvbuf(idx, NULL, _IOFBF, 1 << 20);
        fprintf(idx, "waveview-names 1\n");
        bucket_at[0] = new_bucket("");
        idx_checkpoint();
        char *scopes_path = (char *)malloc(strlen(index_path) + 8);
        if (scopes_path) {
            sprintf(scopes_path, "%s.scopes", index_path);
            idx_scopes = fopen(scopes_path, "w");
            free(scopes_path);
        }
        plus_at[0] = 0;
    }
    if (!idx && list_max)
        bucket_at[0] = new_bucket("");
    if (no_tree) {
        fsdb->ffrSetTreeCBFunc(scopes_cb, NULL);
        if (fsdb->ffrReadScopeTree() != FSDB_RC_SUCCESS || n_scope_off != expect_scopes) {
            fprintf(out, "! the name index does not match %s (%" PRIu64 " scopes, the index has %" PRIu64 ")\n",
                    argv[1], n_scope_off, expect_scopes);
            fflush(out);
            return 1;
        }
        scope_read = (char *)calloc((size_t)n_scope_off + 1, 1);
        if (!scope_read) {
            fprintf(out, "! out of memory for the scopes\n");
            fflush(out);
            return 1;
        }
        declaring = 1;
    }
    fsdb->ffrSetTreeCBFunc(tree_cb, NULL);
    if (!no_tree && fsdb->ffrReadScopeVarTree() != FSDB_RC_SUCCESS) {
        fprintf(out, "! the FSDB reader could not read the scope tree of %s\n", argv[1]);
        fflush(out);
        return 1;
    }
    if (idx) {
        int failed = ferror(idx);
        if (fclose(idx) != 0 || failed) {
            fprintf(out, "! cannot write the name index %s (disk full?)\n", index_path);
            fflush(out);
            return 1;
        }
        idx = NULL;
        if (idx_scopes) {
            fclose(idx_scopes);
            idx_scopes = NULL;
        }
        fprintf(out, "index %" PRIu64 " %" PRIu64 "\n", n_listed, n_scopes);
        for (size_t i = 0; i < n_buckets; i++)
            if (buckets[i].n)
                fprintf(out, "scope %" PRIu64 " %s\n", buckets[i].n, buckets[i].key);
        for (int i = 0; i < n_clocks; i++)
            if (clocks[i].id)
                fprintf(out, "clock %u %" PRIu64 " %s\n", (unsigned)clocks[i].id, clocks[i].ord, clocks[i].name);
    } else if (list_max && n_listed > list_max) {
        /* more than the client would list: it gets the count and the scopes
         * (those below --scope, since only listed variables were counted) */
        fprintf(out, "index %" PRIu64 " %" PRIu64 "\n", n_listed, n_scopes);
        for (size_t i = 0; i < n_buckets; i++)
            if (buckets[i].n)
                fprintf(out, "scope %" PRIu64 " %s\n", buckets[i].n, buckets[i].key);
    }
    fprintf(out, "end\n");
    fflush(out);

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
        } else if (!strcmp(tok[0], "vars") && no_tree) {
            const char *failed = NULL;
            for (int i = 1; i < n && !failed; i++) {
                uint64_t o = strtoull(tok[i], NULL, 10);
                if (o == 0 || o > n_scope_off)
                    failed = "no such scope";
                else if (!scope_read[o]) {
                    if (fsdb->ffrReadVarByLogUOff(&scope_off[o]) != FSDB_RC_SUCCESS)
                        failed = "the FSDB reader could not read the variables of a scope";
                    else
                        scope_read[o] = 1;
                }
            }
            if (failed)
                fprintf(out, "! %s\n", failed);
            else
                fprintf(out, ".\n");
        } else if (!strcmp(tok[0], "stat")) {
            struct stat st;
            if (stat(argv[1], &st) == 0)
                fprintf(out, "s %lld %lld\n.\n", (long long)st.st_size, (long long)st.st_mtime);
            else
                fprintf(out, "! cannot stat %s\n", argv[1]);
        } else if (!strcmp(tok[0], "at") && n >= 3) {
            uint64_t t = strtoull(tok[1], NULL, 10);
            if (t > t_max)
                t = t_max;
            int nh = parse_handles(tok + 2, n - 2, hl);
            run_at(t, hl, nh);
        } else {
            fprintf(out, "! unknown command %s\n", tok[0]);
        }
        fflush(out);
    }
    fsdb->ffrClose();
    return 0;
}
