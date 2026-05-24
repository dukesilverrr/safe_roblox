function! JsonEmoji(c) abort
    let n = char2nr(a:c)
    return n <= 0xffff ? printf('\u%04x', n) : printf('\u%04x\u%04x', 0xd800 + ((n - 0x10000) / 0x400), 0xdc00 + ((n - 0x10000) % 0x400))
endfunction

command! ConvertEmojis %s/./\=char2nr(submatch(0)) > 127 ? JsonEmoji(submatch(0)) : submatch(0)/g
