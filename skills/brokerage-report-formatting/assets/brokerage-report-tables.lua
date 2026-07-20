-- Pipe tables do not carry explicit column widths. Pandoc otherwise emits
-- natural-width longtables, which clip realistic Chinese research content.
function Table(table)
  local count = #table.colspecs
  if count < 2 then
    return table
  end

  local width = 0.94 / count
  for index = 1, count do
    table.colspecs[index] = { table.colspecs[index][1], width }
  end
  return table
end
