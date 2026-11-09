/* Minimal FST <-> VCD converter built on the GTKWave FST library that ships
 * inside Verilator (<VERILATOR_ROOT>/include/gtkwave).
 *
 * It exists so that tools/wave2img.py can read .fst dumps without GTKWave
 * being installed. wave2img.py compiles this file on demand and caches the
 * resulting binary.
 *
 * Usage:
 *     fst2vcd_mini <in.fst> -o <out.vcd>      FST -> VCD
 *     fst2vcd_mini --vcd2fst <in.vcd> -o <out.fst>
 *
 * The FST -> VCD direction is the one the tool needs; fstapi already knows
 * how to spell a VCD, so it is only a few calls. The VCD -> FST direction
 * exists for the wave2img.py self test, which needs an FST to read back. It
 * understands only the VCD subset wave2img.py itself writes and parses:
 * $timescale, $scope, $upscope, $var, $enddefinitions, #<time>, and scalar
 * and vector value changes.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "fstapi.h"

#define MAX_ID 32
#define MAX_LINE 65536

struct vcd_var {
    char id[MAX_ID];
    uint32_t width;
    fstHandle handle;
};

static struct vcd_var *vars = NULL;
static int var_count = 0;
static int var_alloc = 0;

static struct vcd_var *var_find(const char *id)
{
    int i;
    for (i = 0; i < var_count; i++) {
        if (strcmp(vars[i].id, id) == 0) return &vars[i];
    }
    return NULL;
}

static struct vcd_var *var_add(const char *id, uint32_t width, fstHandle handle)
{
    if (var_count == var_alloc) {
        var_alloc = var_alloc ? var_alloc * 2 : 64;
        vars = (struct vcd_var *)realloc(vars, (size_t)var_alloc * sizeof(*vars));
        if (!vars) { fprintf(stderr, "fst2vcd_mini: out of memory\n"); exit(1); }
    }
    snprintf(vars[var_count].id, MAX_ID, "%s", id);
    vars[var_count].width = width;
    vars[var_count].handle = handle;
    return &vars[var_count++];
}

static int fst_to_vcd(const char *in_path, const char *out_path)
{
    void *ctx;
    FILE *fv;

    ctx = fstReaderOpen(in_path);
    if (!ctx) {
        fprintf(stderr, "fst2vcd_mini: cannot open FST file: %s\n", in_path);
        return 1;
    }

    fv = fopen(out_path, "wb");
    if (!fv) {
        fprintf(stderr, "fst2vcd_mini: cannot write: %s\n", out_path);
        fstReaderClose(ctx);
        return 1;
    }

    /* fstReaderProcessHier writes the whole VCD header, and IterBlocks2 with
     * a FILE * and no callbacks writes the value change body. */
    fstReaderProcessHier(ctx, fv);
    fstReaderSetFacProcessMaskAll(ctx);
    fstReaderIterBlocks2(ctx, NULL, NULL, NULL, fv);

    fclose(fv);
    fstReaderClose(ctx);
    return 0;
}

/* Emit one value change into the FST writer. "val" is the VCD payload: a
 * single character for a scalar, or the binary digits for a vector. FST wants
 * the value zero/x/z extended to the declared width. */
static void emit_change(void *ctx, struct vcd_var *v, const char *val)
{
    char buf[8192];
    size_t len = strlen(val);
    size_t pad;
    char fill;

    if (v->width == 1) {
        buf[0] = val[len ? len - 1 : 0];
        buf[1] = '\0';
        fstWriterEmitValueChange(ctx, v->handle, buf);
        return;
    }
    if (len >= v->width) {
        memcpy(buf, val + (len - v->width), v->width);
    } else {
        fill = (val[0] == 'x' || val[0] == 'X' || val[0] == 'z' || val[0] == 'Z')
                   ? val[0]
                   : '0';
        pad = v->width - len;
        memset(buf, fill, pad);
        memcpy(buf + pad, val, len);
    }
    buf[v->width] = '\0';
    fstWriterEmitValueChange(ctx, v->handle, buf);
}

static int vcd_to_fst(const char *in_path, const char *out_path)
{
    FILE *fp;
    void *ctx;
    char line[MAX_LINE];
    int in_header = 1;
    char timescale[64];

    fp = fopen(in_path, "rb");
    if (!fp) {
        fprintf(stderr, "fst2vcd_mini: cannot open VCD file: %s\n", in_path);
        return 1;
    }
    ctx = fstWriterCreate(out_path, 1);
    if (!ctx) {
        fprintf(stderr, "fst2vcd_mini: cannot write: %s\n", out_path);
        fclose(fp);
        return 1;
    }
    fstWriterSetPackType(ctx, FST_WR_PT_ZLIB);
    fstWriterSetFileType(ctx, FST_FT_VERILOG);
    snprintf(timescale, sizeof(timescale), "1ns");

    while (fgets(line, sizeof(line), fp)) {
        char *tok = strtok(line, " \t\r\n");
        while (tok) {
            if (in_header && strcmp(tok, "$timescale") == 0) {
                char acc[64];
                acc[0] = '\0';
                while ((tok = strtok(NULL, " \t\r\n")) && strcmp(tok, "$end") != 0) {
                    if (strlen(acc) + strlen(tok) + 1 < sizeof(acc)) strcat(acc, tok);
                }
                if (acc[0]) fstWriterSetTimescaleFromString(ctx, acc);
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (in_header && strcmp(tok, "$scope") == 0) {
                char *typ = strtok(NULL, " \t\r\n");
                char *nam = strtok(NULL, " \t\r\n");
                (void)typ;
                if (nam) fstWriterSetScope(ctx, FST_ST_VCD_MODULE, nam, NULL);
                while ((tok = strtok(NULL, " \t\r\n")) && strcmp(tok, "$end") != 0) { }
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (in_header && strcmp(tok, "$upscope") == 0) {
                fstWriterSetUpscope(ctx);
                while ((tok = strtok(NULL, " \t\r\n")) && strcmp(tok, "$end") != 0) { }
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (in_header && strcmp(tok, "$var") == 0) {
                char *typ = strtok(NULL, " \t\r\n");
                char *wid = strtok(NULL, " \t\r\n");
                char *id = strtok(NULL, " \t\r\n");
                char *nam = strtok(NULL, " \t\r\n");
                char full[512];
                char *rest;
                uint32_t width;
                struct vcd_var *prev;
                fstHandle alias = 0;
                (void)typ;
                if (!wid || !id || !nam) break;
                width = (uint32_t)strtoul(wid, NULL, 10);
                snprintf(full, sizeof(full), "%s", nam);
                while ((rest = strtok(NULL, " \t\r\n")) && strcmp(rest, "$end") != 0) {
                    if (rest[0] == '[') {
                        size_t l = strlen(full);
                        snprintf(full + l, sizeof(full) - l, " %s", rest);
                    }
                }
                prev = var_find(id);
                if (prev) alias = prev->handle;
                {
                    fstHandle h = fstWriterCreateVar(ctx, FST_VT_VCD_WIRE,
                                                     FST_VD_IMPLICIT, width,
                                                     full, alias);
                    if (!prev) var_add(id, width, h);
                }
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (strcmp(tok, "$enddefinitions") == 0) {
                in_header = 0;
                while ((tok = strtok(NULL, " \t\r\n")) && strcmp(tok, "$end") != 0) { }
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (tok[0] == '$') {
                /* $date, $version, $comment, $dumpvars, $end, ... */
                if (strcmp(tok, "$date") == 0 || strcmp(tok, "$version") == 0 ||
                    strcmp(tok, "$comment") == 0) {
                    while ((tok = strtok(NULL, " \t\r\n")) && strcmp(tok, "$end") != 0) { }
                }
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (tok[0] == '#') {
                fstWriterEmitTimeChange(ctx, strtoull(tok + 1, NULL, 10));
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (tok[0] == 'b' || tok[0] == 'B') {
                char val[8192];
                char *id;
                struct vcd_var *v;
                snprintf(val, sizeof(val), "%s", tok + 1);
                id = strtok(NULL, " \t\r\n");
                if (id && (v = var_find(id)) != NULL) emit_change(ctx, v, val);
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (tok[0] == 'r' || tok[0] == 'R') {
                strtok(NULL, " \t\r\n"); /* identifier; reals are skipped */
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            if (tok[0] == '0' || tok[0] == '1' || tok[0] == 'x' || tok[0] == 'X' ||
                tok[0] == 'z' || tok[0] == 'Z') {
                char val[2];
                struct vcd_var *v;
                val[0] = tok[0];
                val[1] = '\0';
                v = var_find(tok + 1);
                if (v) emit_change(ctx, v, val);
                tok = strtok(NULL, " \t\r\n");
                continue;
            }
            tok = strtok(NULL, " \t\r\n");
        }
    }

    fclose(fp);
    fstWriterClose(ctx);
    return 0;
}

static void usage(void)
{
    fprintf(stderr,
            "usage: fst2vcd_mini <in.fst> -o <out.vcd>\n"
            "       fst2vcd_mini --vcd2fst <in.vcd> -o <out.fst>\n");
}

int main(int argc, char **argv)
{
    const char *in_path = NULL;
    const char *out_path = NULL;
    int reverse = 0;
    int i;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--vcd2fst") == 0) {
            reverse = 1;
        } else if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
            out_path = argv[++i];
        } else if (argv[i][0] != '-') {
            if (!in_path) in_path = argv[i];
        } else {
            usage();
            return 2;
        }
    }
    if (!in_path || !out_path) {
        usage();
        return 2;
    }
    return reverse ? vcd_to_fst(in_path, out_path) : fst_to_vcd(in_path, out_path);
}
