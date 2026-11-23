# The original hub's circuit board

Notes read from photos of both sides of a MySmartBlinds Smart Bridge.
Silkscreen: **MySmartBlinds Hub MSB-G006-00 V1.1**. Sold as model
MSB-G012-00. Nothing on this board has been probed or powered for testing.

BlindBridge does not use this board. A Raspberry Pi does the job. These notes
are kept for anyone who wants to try reviving the original.

| Ref | Part | Notes |
| --- | --- | --- |
| U6 | Inventek eS-WiFi, label "43362 M3G 1749", Rev B | ISM43362-M3G-L44: Broadcom/Cypress BCM43362 Wi-Fi radio plus an STM32F205 processor with 1 MB flash. Inventek ships it with either their AT-command firmware or custom WICED firmware. Which one is on this board is not known. |
| U4 | Raytac MDBT42Q (FCC ID SH6MDBT42Q) | Nordic nRF52 Bluetooth module, almost certainly nRF52832. |
| U7 | TI TLC59116 ("Y59116") | 16-channel I2C LED driver for the four RGB LEDs D1 to D4. |
| R7, R8 | 10k resistors between U4 and U7 | Probably the I2C pull-ups, which would put the LED driver on the Bluetooth processor. Unproven. |
| U5, U8 | Macronix MX25L1606E | 2 MB SPI flash, two of them. |
| U2 | Marked "UL26" | USB data-line protection (USBLC6-2 family). Suggests the micro-USB data lines are in use, most likely to the STM32. |
| U3 | 3.3 V regulator | |
| U9 | Small sensor, corner | Unidentified. |
| SW1 | Push button, centre | |
| X1 | Crystal next to the Bluetooth module | |
| J3 | Micro-USB jack | The only part on the back. |
| J1 | 2x5 through-hole pads beside U4, about 1.27 mm pitch, square pad = pin 1 | Probably the debug (SWD) port for the Bluetooth processor. Pinout unconfirmed. |
| J2 | 2x5 through-hole pads beside U6, same layout | Probably the debug (JTAG/SWD) port for the Wi-Fi module's processor. Pinout unconfirmed. |

## Open questions

1. Which processor runs the show: the STM32 inside the Wi-Fi module or the nRF52?
2. How the two are wired together (UART or SPI, which pins).
3. Whether either processor has read-out protection switched on.
4. J1 and J2 pinout.
5. Whether the hub shows up as a USB device when plugged into a computer.

## If someone revives it

- A spare Raspberry Pi can act as the debug probe through its GPIO pins (OpenOCD), so no extra tool is needed.
- Inventek publishes its AT-command firmware for this Wi-Fi module. If the module runs it, or can be flashed with it through J2, only the Bluetooth processor needs new firmware.
- New firmware would only have to speak the sync protocol in `PROTOCOL.md` to work with the BlindBridge server unchanged.
