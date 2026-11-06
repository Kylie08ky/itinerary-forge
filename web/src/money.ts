const SYMBOLS: Record<string, string> = { EUR: "€", CNY: "¥", USD: "$", GBP: "£", JPY: "JP¥", CHF: "CHF ", KRW: "₩", THB: "฿" };

/** 币种代码 → 符号；不认识的直接写代码（「AUD 12」）。 */
export function currencySymbol(code: string | undefined | null): string {
  if (!code) return "€";
  return SYMBOLS[code] ?? `${code} `;
}

/** 门票合计的显示：没有价格数据（现建的城市）→「待补」，不写 0；币种跟目的地。 */
export function ticketTotal(amount: number | null | undefined, currency: string | undefined | null): string {
  return amount == null ? "待补" : `${currencySymbol(currency)}${Math.round(amount)}`;
}
