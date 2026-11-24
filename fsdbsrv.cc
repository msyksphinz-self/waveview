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
 * The FsdbReader library writes log lines to stdout. The protocol therefore
 * goes to a duplicate of the original stdout, and file descriptor 1 is
 * pointed at a scratch file before the library is touched.
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

static int mode_agg;
static uint64_t q_t0, q_t1;
static int q_level;

/* -- hierarchy ------------------------------------------------------------ */

static char path[MAX_LINE];
static size_t depth_len[MAX_DEPTH];
static int depth;

static void push_scope(const char *name)
{
    size_t len = strlen(path);
    if (depth < MAX_DEPTH)
        depth_len[depth] = len;
    depth++;
    if (len)
        strncat(path, ".", sizeof(path) - strlen(path) - 1);
    strncat(path, name ? name : "", sizeof(path) - strlen(path) - 1);
}

static void pop_scope(void)
{
    if (depth > 0) {
        depth--;
        if (depth < MAX_DEPTH)
            path[depth_len[depth]] = 0;
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
    uint32_t w = (uint32_t)(var->lbitnum >= var->rbitnum ? var->lbitnum - var->rbitnum : var->rbitnum - var->lbitnum) + 1;
    width_of[id] = w;
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
    fprintf(out, "var %u %u %s%s%s\n", (unsigned)id, w, path, path[0] ? "." : "", name);
}

static bool_T tree_cb(fsdbTreeCBType cb_type, void *client_data, void *tree_cb_data)
{
    (void)client_data;
    switch (cb_type) {
    case FSDB_TREE_CBT_SCOPE:
        push_scope(((fsdbTreeCBDataScope *)tree_cb_data)->name);
        break;
    case FSDB_TREE_CBT_STRUCT_BEGIN:
        push_scope(((fsdbTreeCBDataStructBegin *)tree_cb_data)->name);
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

static void load(fsdbVarIdcode *hl, int nh)
{
    fsdb->ffrResetSignalList();
    for (int i = 0; i < nh; i++)
        fsdb->ffrAddToSignalList(hl[i]);
    fsdb->ffrLoadSignals();
}

static void unload(void)
{
    fsdb->ffrUnloadSignals();
    fsdb->ffrResetSignalList();
}

static void run_query(fsdbVarIdcode *hl, int nh)
{
    load(hl, nh);
    for (int i = 0; i < nh; i++)
        serve_one(hl[i]);
    unload();
    fprintf(out, ".\n");
}

static void run_at(uint64_t t, fsdbVarIdcode *hl, int nh)
{
    load(hl, nh);
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
    if (argc < 2) {
        fprintf(stderr, "usage: fsdbsrv <dump.fsdb>\n");
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
    fsdb->ffrReadDataTypeDefByBlkIdx(0); /* keeps transaction data from tripping the reader */

    fsdbTag64 tag;
    t_min = fsdb->ffrGetMinFsdbTag64(&tag) == FSDB_RC_SUCCESS ? T2U64(tag) : 0;
    t_max = fsdb->ffrGetMaxFsdbTag64(&tag) == FSDB_RC_SUCCESS ? T2U64(tag) : t_min;

    max_id = fsdb->ffrGetMaxVarIdcode();
    width_of = (uint32_t *)calloc((size_t)max_id + 1, sizeof(*width_of));
    hs = (struct hstate *)calloc((size_t)max_id + 1, sizeof(*hs));
    valbuf = (char *)malloc(1 << 20);

    fprintf(out, "wavesrv 1\n");
    fprintf(out, "timescale %d\n", timescale_exp());
    fprintf(out, "range %" PRIu64 " %" PRIu64 "\n", t_min, t_max);
    fsdb->ffrSetTreeCBFunc(tree_cb, NULL);
    fsdb->ffrReadScopeVarTree();
    fprintf(out, "end\n");
    fflush(out);

    char *line = (char *)malloc(MAX_LINE);
    char **tok = (char **)malloc(sizeof(char *) * (MAX_LINE / 2));
    fsdbVarIdcode *hl = (fsdbVarIdcode *)malloc(sizeof(fsdbVarIdcode) * (MAX_LINE / 2));
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
