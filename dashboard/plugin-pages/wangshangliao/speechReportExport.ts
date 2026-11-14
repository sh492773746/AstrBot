export type ReportMember = {
  id: string; name: string; counts: number[]; total: number; hours?: number[][];
};
export type SpeechReport = {
  configured: boolean; catching_up?: boolean; updated_at?: number;
  groups?: Array<{ id: string; name: string }>;
  days?: Array<{ date: string; total: number; totals: number[]; members: ReportMember[] }>;
};
export type ReportTable = {
  title: string; subtitle: string; columns: string[]; rows: string[][]; footer: string[];
};
export type ReportView = "summary" | "hourly";

export function reportTable(
  report: SpeechReport, day: number, view: ReportView, groupId = "",
): ReportTable | null {
  const selected = report.days?.[day], groups = report.groups;
  if (!selected || !groups?.length) return null;
  const index = groupId ? groups.findIndex(group => group.id === groupId) : -1;
  if (groupId && index < 0) return null;
  const clean = (value: string) => value.replace(/[\s\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+/g, " ").trim();
  const count = (counts: number[]) => index < 0 ? counts.reduce((sum, n) => sum + n, 0) : counts[index];
  let columns: string[], rows: string[][];
  if (view === "hourly") {
    if (selected.members.some(member => member.hours?.length !== 24 ||
      member.hours.some(values => values.length !== groups.length))) return null;
    columns = ["北京时间段", ...selected.members.map(member => `${clean(member.name)}（${member.id}）`), "合计"];
    rows = Array.from({ length: 24 }, (_, hour) => {
      const counts = selected.members.map(member => count(member.hours![hour]));
      return [`${String(hour).padStart(2, "0")}:00-${String(hour + 1).padStart(2, "0")}:00`,
        ...counts.map(String), String(counts.reduce((sum, n) => sum + n, 0))];
    });
    const totals = selected.members.map(member => count(member.counts));
    rows.push(["全日合计", ...totals.map(String), String(totals.reduce((sum, n) => sum + n, 0))]);
  } else {
    const visible = groups.map((group, i) => ({ ...group, index: i })).filter(group => index < 0 || group.index === index);
    columns = ["成员（UID）", ...visible.map(group => `${clean(group.name)}（${group.id}）`), "合计"];
    rows = selected.members.map(member => [
      `${clean(member.name)}（${member.id}）`,
      ...visible.map(group => String(member.counts[group.index])), String(count(member.counts)),
    ]);
    rows.push(["合计", ...visible.map(group => String(selected.totals[group.index])), String(count(selected.totals))]);
  }
  const updated = report.updated_at ? new Date(report.updated_at * 1000).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", hour12: false,
  }) : "尚未核对";
  return {
    title: `专门发言汇报 · ${selected.date}`,
    subtitle: `北京时间 · ${index < 0 ? "全部统计群合计" : `${clean(groups[index].name)}（${groups[index].id}）`} · ${view === "hourly" ? "每小时明细" : "每日汇总"}`,
    columns, rows,
    footer: [
      "仅计有效文字；撤回、违规、机器人、管理命令和图片／视频／转发不计。",
      `最近核对：${updated}；最近30秒待核对；仅保留今日和昨日。`,
      ...(report.catching_up ? ["数据补齐中，当前结果尚不完整。"] : []),
    ],
  };
}

export function reportTsv(table: ReportTable): string {
  const cell = (value: string) => /^[=+@-]/.test(value) ? "'" + value : value;
  return [table.title, table.subtitle, table.columns.map(cell).join("\t"),
    ...table.rows.map(row => row.map(cell).join("\t")), ...table.footer].join("\n");
}

export function reportSvg(table: ReportTable): string {
  const escape = (value: string) => value.replace(/[&<>"']/g, char =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[char]!);
  const wrap = (value: string, length: number) => {
    const chars = Array.from(value), lines: string[] = [];
    for (let i = 0; i < chars.length; i += length) lines.push(chars.slice(i, i + length).join(""));
    return lines.length ? lines : [""];
  };
  const widths = table.columns.map((_, index) => index === 0 ? 200 : 160);
  const width = Math.max(760, widths.reduce((sum, value) => sum + value, 0) + 48);
  let y = 36;
  const elements: string[] = [];
  for (const [index, text] of [table.title, table.subtitle].entries()) {
    const size = index ? 13 : 20;
    for (const line of wrap(text, Math.floor((width - 48) / size))) {
      elements.push(`<text x="24" y="${y}" font-size="${size}" font-weight="${index ? 400 : 600}">${escape(line)}</text>`);
      y += size + 10;
    }
  }
  y += 6;
  for (const [rowIndex, row] of [table.columns, ...table.rows].entries()) {
    const lines = row.map((cell, index) => wrap(cell, index === 0 ? 12 : 9));
    const height = Math.max(36, ...lines.map(cell => cell.length * 20 + 16));
    const header = rowIndex === 0, total = rowIndex === table.rows.length;
    elements.push(`<rect x="24" y="${y}" width="${width - 48}" height="${height}" fill="${header ? "#edf1f4" : total ? "#e9f3ed" : rowIndex % 2 ? "#ffffff" : "#f8fafb"}"/>`);
    let x = 24;
    lines.forEach((cell, index) => {
      const left = index === 0 || header;
      elements.push(`<text x="${left ? x + 12 : x + widths[index] - 12}" y="${y + 24}" text-anchor="${left ? "start" : "end"}" font-size="14" font-weight="${header || total ? 600 : 400}">` +
        cell.map((line, i) => `<tspan x="${left ? x + 12 : x + widths[index] - 12}" dy="${i ? 20 : 0}">${escape(line)}</tspan>`).join("") + "</text>");
      x += widths[index];
    });
    y += height;
    elements.push(`<path d="M24 ${y}H${width - 24}" stroke="#d9dfe5"/>`);
  }
  y += 26;
  for (const note of table.footer) {
    for (const line of wrap(note, Math.floor((width - 48) / 13))) {
      elements.push(`<text x="24" y="${y}" font-size="12" fill="#52616b">${escape(line)}</text>`);
      y += 20;
    }
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${y + 8}" viewBox="0 0 ${width} ${y + 8}" role="img" aria-labelledby="title"><title id="title">${escape(table.title)} ${escape(table.subtitle)}</title><rect width="100%" height="100%" fill="#ffffff"/><g font-family="Arial, 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif" fill="#202b33">${elements.join("")}</g></svg>`;
}

export async function reportPng(table: ReportTable, signal: AbortSignal): Promise<Blob> {
  if (signal.aborted) throw new DOMException("Export cancelled", "AbortError");
  const canvas = document.createElement("canvas");
  const image = new Image();
  const url = URL.createObjectURL(new Blob([reportSvg(table)], { type: "image/svg+xml;charset=utf-8" }));
  let timer: ReturnType<typeof setTimeout> | undefined;
  let abort: () => void = () => {};
  try {
    return await new Promise<Blob>((resolve, reject) => {
      abort = () => reject(new DOMException("Export cancelled", "AbortError"));
      signal.addEventListener("abort", abort, { once: true });
      timer = setTimeout(() => reject(new Error("Image export timed out")), 10_000);
      image.onerror = () => reject(new Error("Report image could not be decoded"));
      image.onload = () => {
        try {
          const width = image.naturalWidth, height = image.naturalHeight;
          if (!width || !height || width > 4096 || height > 4096 || width * height > 4_000_000) {
            throw new Error("Report image exceeds pixel limit");
          }
          canvas.width = width; canvas.height = height;
          const context = canvas.getContext("2d");
          if (!context) throw new Error("Canvas is unavailable");
          context.drawImage(image, 0, 0);
          canvas.toBlob(blob => {
            if (!blob || blob.type !== "image/png" || blob.size > 2 * 1024 * 1024) {
              reject(new Error("Report image could not be encoded within size limit"));
            } else {
              resolve(blob);
            }
          }, "image/png");
        } catch (error) {
          reject(error);
        }
      };
      image.src = url;
    });
  } finally {
    if (timer) clearTimeout(timer);
    signal.removeEventListener("abort", abort);
    image.onload = null; image.onerror = null;
    image.src = "";
    URL.revokeObjectURL(url);
    // The report exists only in browser memory, never in server-side storage.
    canvas.width = 0; canvas.height = 0;
  }
}
