-- Pipe tables carry no explicit column widths. Pandoc would emit natural-width
-- longtables that clip realistic Chinese research content, so we assign widths
-- ourselves.
--
-- Equal division used to be the rule, but it wastes space on short columns
-- (编号 / 类型 hold 3-4 characters) while starving the columns that hold source
-- ids, domains and evidence sentences — those then produce Overfull \hbox and
-- fail the delivery gate. We instead weight each column by the content it
-- actually carries.

local stringify = pandoc.utils.stringify

local MIN_WEIGHT = 4        -- keep narrow columns readable
local MAX_WEIGHT = 46       -- stop one long sentence from crushing the rest
local TOTAL_WIDTH = 0.96    -- leave a little slack for \tabcolsep

--- Visual width in half-width units: CJK glyphs occupy two columns.
--- Falls back to a byte-based estimate when the text is not valid UTF-8,
--- because a malformed cell must not abort the whole conversion.
local function visual_width(text)
  local ok, width = pcall(function()
    local total = 0
    for _, code in utf8.codes(text) do
      if code >= 0x1100 and (
        code <= 0x115F
        or (code >= 0x2E80 and code <= 0xA4CF)
        or (code >= 0xAC00 and code <= 0xD7A3)
        or (code >= 0xF900 and code <= 0xFAFF)
        or (code >= 0xFE30 and code <= 0xFE6F)
        or (code >= 0xFF00 and code <= 0xFF60)
        or (code >= 0xFFE0 and code <= 0xFFE6)
      ) then
        total = total + 2
      else
        total = total + 1
      end
    end
    return total
  end)
  if ok then
    return width
  end
  -- Roughly: CJK is 3 bytes wide and renders 2 columns.
  return math.floor(#text * 2 / 3 + 0.5)
end

--- Longest unbreakable run decides the minimum comfortable column width.
local function longest_token(text)
  local longest = 0
  for token in text:gmatch("%S+") do
    local width = visual_width(token)
    if width > longest then longest = width end
  end
  return longest
end

function Table(tbl)
  local count = #tbl.colspecs
  if count < 2 then
    return tbl
  end

  local weights = {}
  for index = 1, count do weights[index] = MIN_WEIGHT end

  local function observe(row)
    for index, cell in ipairs(row.cells) do
      if index <= count then
        local ok, text = pcall(stringify, cell.contents)
        if ok and text then
          -- A column must fit its longest single token, but overall sizing
          -- follows total length so sentence-heavy columns still get more room.
          local demand = math.max(longest_token(text), visual_width(text) / 3)
          if demand > weights[index] then weights[index] = demand end
        end
      end
    end
  end

  if tbl.head then
    for _, row in ipairs(tbl.head.rows) do observe(row) end
  end
  for _, body in ipairs(tbl.bodies) do
    for _, row in ipairs(body.body) do observe(row) end
  end
  if tbl.foot then
    for _, row in ipairs(tbl.foot.rows) do observe(row) end
  end

  local total = 0
  for index = 1, count do
    if weights[index] > MAX_WEIGHT then weights[index] = MAX_WEIGHT end
    total = total + weights[index]
  end
  if total <= 0 then
    return tbl
  end

  for index = 1, count do
    tbl.colspecs[index] = {
      tbl.colspecs[index][1],
      TOTAL_WIDTH * weights[index] / total,
    }
  end
  return tbl
end
