- Card `679a6d6e`: consume bounded owner-authorized crew and support requests from
  the existing node loop, preserving parent ownership and coalescing duplicate
  resource work. Persist delivery evidence and reject helpers whose parent
  generation or restrictions changed before dispatch. Existing fleet pause wins.
