const KEY = 'jagent.ui.theme'

export type Theme = 'dark' | 'light'

export function readTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    /* private mode: the attribute is enough */
  }
}
