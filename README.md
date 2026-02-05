# STM32 Remote Flasher

A lightweight, SSH-based system for **remote flashing, recovery, and orchestration
of STM32 devices** connected to Linux flash servers (e.g. Raspberry Pi).

This repository is structured to support:
- Remote STM32 flashing (USB DFU, SWD via ST-LINK)
- Automated recovery using reset lines or USB hub power control
- Parallel programming for small device farms or labs
- Hardware-backed automation (custom USB hub)

---

## Repository Structure

```text
stm32-remote-flasher/
├── stm32-remote-flasher_scripts/   # Python tooling (core project)
│   ├── stm_remote.py
│   ├── update_config.py
│   └── README.md                   # Detailed script documentation
├── hub_hw/                         # (planned) USB hub hardware design
├── hub_fw/                         # (planned) USB hub firmware
└── LICENSE

