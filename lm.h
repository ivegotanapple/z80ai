#ifndef LM_H
#define LM_H
#include <stdint.h>

#define LM_MAX_OUT 50            /* maximum reply length, same as the Z80 .COM */

/* Starts a generation for the given text (NUL-terminated string). */
void lm_start(const char *text);

/* Computes the next character. Returns its ASCII code, or 0 at the end of the reply
 * (EOS reached, or LM_MAX_OUT characters produced). */
char lm_step(void);

#endif
