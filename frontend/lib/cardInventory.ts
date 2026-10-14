import { Card } from '../types';

export const GROUPED_CARD_SEPARATOR = '======== CARD ========';
export const GROUPED_CARD_EXAMPLE = '--- UPDEVNEWS MAIL ---\nUsername: account1\nEmail: account1@example.com\nPassword: xxx\n\n--- PROTON ---\nEmail: mail1@example.com\nPassword: xxx\n\n--- EPIC ---\nAccount: mail1@example.com\nPassword: xxx\n\n======== CARD ========\n\n--- UPDEVNEWS MAIL ---\nUsername: account2\nEmail: account2@example.com\nPassword: xxx\n\n--- PROTON ---\nEmail: mail2@example.com\nPassword: xxx\n\n--- EPIC ---\nAccount: mail2@example.com\nPassword: xxx';

export const isInventoryCard = (type?: Card['type']) => type === 'data' || type === 'grouped_data';

export const getCardStockCount = (type?: Card['type'], content = ''): number => {
  if (type === 'data') return content.split('\n').filter(line => line.trim()).length;
  if (type !== 'grouped_data') return 0;
  let count = 0;
  let hasContent = false;
  for (const line of content.split(/\r\n|\r|\n/)) {
    if (line.trim() === GROUPED_CARD_SEPARATOR) {
      if (hasContent) count += 1;
      hasContent = false;
    } else if (line.trim()) {
      hasContent = true;
    }
  }
  return count + (hasContent ? 1 : 0);
};
