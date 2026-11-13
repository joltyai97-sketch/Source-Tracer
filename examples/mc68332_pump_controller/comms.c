/*
 * comms.c - Status frame over the SCI (RS-485, 9600 8N1).
 *
 * Frame: 0x7E | ID | LEN | fault | flags_hi | flags_lo | temp | vbus_hi | vbus_lo | csum
 */
#include "regs.h"
#include "faults.h"

#define FRAME_START   0x7E
#define STATUS_ID     0x01
#define STATUS_LEN    6

extern short          g_temp_c;
extern unsigned short g_vbus_mv;

unsigned char g_tx_buf[16];

/* Old-style (K&R) definition kept from the original code base */
unsigned char checksum(buf, len)
unsigned char *buf;
int len;
{
    unsigned char sum = 0;
    while (len--)
        sum += *buf++;
    return (unsigned char)(~sum + 1);
}

static void sci_putc(unsigned char c)
{
    while (!(SCSR & SCSR_TDRE))
        ;
    SCDR = c;
}

void sci_init(void)
{
    SCCR0 = 0x0037;      /* 9600 baud @ 16.78 MHz */
    SCCR1 = 0x000C;      /* TE | RE */
}

void comms_send_status(void)
{
    int i, n = 0;

    g_tx_buf[n++] = FRAME_START;
    g_tx_buf[n++] = STATUS_ID;
    g_tx_buf[n++] = STATUS_LEN;
    g_tx_buf[n++] = (unsigned char)g_active_fault;
    g_tx_buf[n++] = (unsigned char)(g_fault_flags >> 8);
    g_tx_buf[n++] = (unsigned char)(g_fault_flags & 0xFF);
    g_tx_buf[n++] = (unsigned char)(g_temp_c + 40);
    g_tx_buf[n++] = (unsigned char)(g_vbus_mv >> 8);
    g_tx_buf[n++] = (unsigned char)(g_vbus_mv & 0xFF);
    g_tx_buf[n] = checksum(&g_tx_buf[1], n - 1);
    n++;

    for (i = 0; i < n; i++)
        sci_putc(g_tx_buf[i]);
}
