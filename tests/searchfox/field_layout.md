Field Layout: mozilla::detail::nsTStringRepr

Size: 16 bytes, Alignment: 8 bytes

Fields:
╭────────┬──────┬─────────────────────────────────────────────────────────┬─────────────╮
│ [36moffset[39m │ [36msize[39m │ [36mtype[39m                                                    │ [36mname[39m        │
├────────┼──────┼─────────────────────────────────────────────────────────┼─────────────┤
│ 0      │ 8    │ char16_t *                                              │ mData       │
│ 8      │ 4    │ class mozilla::detail::nsTStringLengthStorage<char16_t> │ mLength     │
│ 12     │ 2    │ enum mozilla::detail::StringDataFlags                   │ mDataFlags  │
│ 14     │ 2    │ const enum mozilla::detail::StringClassFlags            │ mClassFlags │
╰────────┴──────┴─────────────────────────────────────────────────────────┴─────────────╯
