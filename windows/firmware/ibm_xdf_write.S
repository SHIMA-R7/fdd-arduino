#include <avr/io.h>

// 16 clocks per MFM cell at 16 MHz. D9 (PB1) emits the active-low
// WRITE DATA pulse, held low for three clocks. Each CELL leaves
// 11 clocks for fixed-time work.
.macro CELL value, bit
    sbrc \value, \bit
    out _SFR_IO_ADDR(PORTB), r23
    rjmp .+0
    out _SFR_IO_ADDR(PORTB), r22
.endm

.macro PAD count
    .rept \count
        nop
    .endr
.endm

.macro BLANK value, bit
    CELL \value, \bit
    PAD 11
.endm

// Three cells service RX. Both paths take 11 clocks per cell. The
// writer pointer is wrapped before it is compared with the reader.
.macro RECEIVE value, bit
    CELL \value, \bit
    lds r16, _SFR_MEM_ADDR(UCSR0A)
    sbrs r16, RXC0
    rjmp 1f
    lds r17, _SFR_MEM_ADDR(UDR0)
    mov r0, r16
    andi r16, (1<<FE0)|(1<<DOR0)|(1<<UPE0)
    breq 2f
    rjmp .Lrx_error
1:  clr r0
    PAD 4
2:  PAD 1
.endm

.macro ENQUEUE value, bit
    CELL \value, \bit
    sbrs r0, RXC0
    rjmp 1f
    st Y+, r17
    cp r28, r8
    cpc r29, r9
    breq 2f
    PAD 2
    rjmp 3f
2:  movw r28, r6
    rjmp 3f
1:  PAD 8
3:
.endm

.macro CHECKFULL value, bit
    CELL \value, \bit
    sbrs r0, RXC0
    rjmp 1f
    cp r28, r26
    cpc r29, r27
    brne 2f
    rjmp .Lrx_error
1:  PAD 3
2:  PAD 5
.endm

.section .text.ibm_xdf_write_mfm,"ax",@progbits
.global ibm_xdf_write_mfm
.type ibm_xdf_write_mfm, @function
// Ring in r25:r24, byte count after the data mark in r23:r22.
// The 512 preloaded host bytes begin at offset 1.
// The caller holds WRITE GATE and disables interrupts for this routine.
ibm_xdf_write_mfm:
    push r6
    push r7
    push r8
    push r9
    push r14
    push r15
    push r16
    push r17
    push r28
    push r29
    movw r6, r24
    movw r8, r24
    ldi r16, 4
    add r9, r16
    movw r26, r24
    adiw r26, 1
    movw r28, r26
    ldi r16, 2
    add r29, r16
    // Remaining bytes after the known data mark, including CRC and gap.
    movw r24, r22
    in r22, _SFR_IO_ADDR(PORTB)
    ori r22, (1<<PB1)
    mov r23, r22
    andi r23, ~(1<<PB1)
    ldi r19, 0xaa
    ldi r21, 12
.Lzeros:
    .irp b,7,6,5,4,3,2,1,0,7,6,5,4,3,2,1
        BLANK r19, \b
    .endr
    CELL r19, 0
    dec r21
    breq 1f
    PAD 7
    rjmp .Lzeros
1:  ldi r19, 0x44
    ldi r18, 0x89
    PAD 6

    .rept 2
        .irp b,7,6,5,4,3,2,1,0
            BLANK r19, \b
        .endr
        .irp b,7,6,5,4,3,2,1,0
            BLANK r18, \b
        .endr
    .endr

    .irp b,7,6,5,4,3,2,1,0
        BLANK r19, \b
    .endr
    .irp b,7,6,5,4,3,2,1
        BLANK r18, \b
    .endr
    CELL r18, 0
    ldi r16, 0xa6
    sts _SFR_MEM_ADDR(UDR0), r16
    ldi r19, 0x55
    ldi r18, 0x45
    ldi r21, 0xfb
    PAD 5

.Ldata:
    RECEIVE r19, 7
    ENQUEUE r19, 6
    CHECKFULL r19, 5
    BLANK r19, 4
    BLANK r19, 3
    BLANK r19, 2
    BLANK r19, 1
    BLANK r19, 0
    RECEIVE r18, 7
    ENQUEUE r18, 6
    CHECKFULL r18, 5

    // Fetch the next data byte and advance the consumer. On the last
    // encoded byte there is no next byte to fetch.
    CELL r18, 4
    sbiw r24, 1
    brcc 1f
    rjmp .Llast
1:
    cp r26, r28
    cpc r27, r29
    brne 1f
    rjmp .Lrx_error
1:  ld r20, X+
    PAD 1

    CELL r18, 3
    cp r26, r8
    cpc r27, r9
    brne 1f
    movw r26, r6
    rjmp 2f
1:  PAD 2
2:  mov r30, r20
    ldi r31, hi8(.Lmfm_hi)
    lpm r14, Z

    CELL r18, 2
    ldi r31, hi8(.Lmfm_lo)
    lpm r15, Z
    ldi r16, 0x7f
    sbrc r21, 0
    and r14, r16
    mov r21, r20
    PAD 3

    BLANK r18, 1
    CELL r18, 0
    // Table result is high byte in r14 and low byte in r15.
    mov r19, r14
    mov r18, r15
    PAD 7
    rjmp .Ldata

.Llast:
    PAD 6
    BLANK r18, 3
    BLANK r18, 2
    BLANK r18, 1
    CELL r18, 0
    clr r24
    rjmp .Lfinish
.Lrx_error:
    ldi r24, 10
.Lfinish:
    // Turn off WRITE GATE immediately. Caller restores the UART.
    cbi _SFR_IO_ADDR(DDRB), PB2
    out _SFR_IO_ADDR(PORTB), r22
    pop r29
    pop r28
    pop r17
    pop r16
    pop r15
    pop r14
    pop r9
    pop r8
    pop r7
    pop r6
    ret
.size ibm_xdf_write_mfm, .-ibm_xdf_write_mfm

// Each table maps one data byte to one half of its encoded channel
// bits, assuming the preceding data bit is zero. The caller supplies
// continuity across bytes by clearing the first clock bit as needed.
.section .progmem.data,"a",@progbits
.balign 256
.Lmfm_hi:
.set byte_value, 0
.rept 256
    .set encoded, 0
    .set preceding, 0
    .irp bit,7,6,5,4
        .set data_bit, (byte_value >> \bit) & 1
        .set encoded, (encoded << 2) | ((1 ^ (data_bit | preceding)) << 1) | data_bit
        .set preceding, data_bit
    .endr
    .byte encoded
    .set byte_value, byte_value+1
.endr
.Lmfm_lo:
.set byte_value, 0
.rept 256
    .set encoded, 0
    .set preceding, (byte_value >> 4) & 1
    .irp bit,3,2,1,0
        .set data_bit, (byte_value >> \bit) & 1
        .set encoded, (encoded << 2) | ((1 ^ (data_bit | preceding)) << 1) | data_bit
        .set preceding, data_bit
    .endr
    .byte encoded
    .set byte_value, byte_value+1
.endr
