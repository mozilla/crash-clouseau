>>> 1055:   [[nodiscard]] value_type& ElementAt(index_type aIndex) {
    1056:     if (MOZ_UNLIKELY(aIndex >= Length())) {
    1057:       mozilla::detail::InvalidArrayIndex_CRASH(aIndex, Length());
    1058:     }
    1059:     return Elements()[aIndex];
    1060:   }

>>> 1066:   [[nodiscard]] const value_type& ElementAt(index_type aIndex) const {
    1067:     if (MOZ_UNLIKELY(aIndex >= Length())) {
    1068:       mozilla::detail::InvalidArrayIndex_CRASH(aIndex, Length());
    1069:     }
    1070:     return Elements()[aIndex];
    1071:   }
