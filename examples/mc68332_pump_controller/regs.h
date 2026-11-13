/*
 * regs.h - MC68332 on-chip register definitions used by the pump controller.
 * Module base 0xFFF000 (SIMCR.MM = 1).
 */
#ifndef REGS_H
#define REGS_H

typedef volatile unsigned char  vu8;
typedef volatile unsigned short vu16;

#define REG8(a)   (*(vu8  *)(a))
#define REG16(a)  (*(vu16 *)(a))

#define MODULE_BASE   0xFFF000

/* ---- SIM ---- */
#define SIMCR   (*(volatile unsigned short *)0xFFFA00)
#define SYNCR   (*(volatile unsigned short *)0xFFFA04)
#define SYPCR   (*(volatile unsigned char  *)0xFFFA21)
#define PICR    (*(volatile unsigned short *)0xFFFA22)
#define PITR    (*(volatile unsigned short *)0xFFFA24)
#define SWSR    (*(volatile unsigned char  *)0xFFFA27)
#define PORTE   REG8(0xFFFA11)
#define DDRE    REG8(0xFFFA15)
#define PORTF   REG8(MODULE_BASE + 0xA19)

/* ---- QSM: SCI ---- */
#define SCCR0   REG16(0xFFFC08)
#define SCCR1   REG16(0xFFFC0A)
#define SCSR    REG16(0xFFFC0C)
#define SCDR    REG16(0xFFFC0E)

#define SCSR_TDRE   0x0100
#define SCSR_TC     0x0080
#define SCSR_RDRF   0x0040

/* ---- QSM: QSPI (external 12-bit ADC on PCS0) ---- */
#define SPCR1      REG16(0xFFFC1A)
#define SPSR       REG8(0xFFFC1F)
#define QSPI_RR    ((vu16 *)0xFFFD00)
#define QSPI_TR    ((vu16 *)0xFFFD20)
#define SPSR_SPIF  0x80

/* ---- TPU ---- */
#define CFSR3   REG16(0xFFFE12)
#define CIER    REG16(0xFFFE0A)

#endif
