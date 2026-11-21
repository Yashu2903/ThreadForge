/**
 * One icon set for the whole application: 24x24 line icons drawn here (stroke = currentColor, so an icon takes the colour
 * of its button and of the theme). Every icon is decorative (`aria-hidden`): the control that holds it carries the name.
 */
import type { ReactNode, SVGProps } from "react";

type Props = SVGProps<SVGSVGElement> & { size?: number };

function icon(paths: ReactNode) {
  return function Icon({ size = 18, ...rest }: Props) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
        {...rest}
      >
        {paths}
      </svg>
    );
  };
}

export const IconPlus = icon(<path d="M12 5v14M5 12h14" />);
export const IconClose = icon(<path d="M6 6l12 12M18 6L6 18" />);
export const IconCheck = icon(<path d="M5 12.5l4.5 4.5L19 7.5" />);
export const IconChevronDown = icon(<path d="M6 9l6 6 6-6" />);
export const IconChevronRight = icon(<path d="M9 6l6 6-6 6" />);
export const IconMore = icon(
  <>
    <circle cx="5" cy="12" r="1.2" fill="currentColor" />
    <circle cx="12" cy="12" r="1.2" fill="currentColor" />
    <circle cx="19" cy="12" r="1.2" fill="currentColor" />
  </>,
);
export const IconMenu = icon(<path d="M4 7h16M4 12h16M4 17h16" />);
export const IconHome = icon(<path d="M4 11l8-7 8 7M6 10v9h12v-9" />);
export const IconHash = icon(<path d="M9 4L7 20M17 4l-2 16M4 9h16M3 15h16" />);
export const IconDoc = icon(
  <>
    <path d="M7 3h7l5 5v13H7z" />
    <path d="M14 3v5h5M10 13h6M10 17h6" />
  </>,
);
export const IconFile = icon(
  <>
    <path d="M7 3h7l5 5v13H7z" />
    <path d="M14 3v5h5" />
  </>,
);
export const IconFilePdf = icon(
  <>
    <path d="M7 3h7l5 5v13H7z" />
    <path d="M14 3v5h5M9.5 17v-4h1.4a1.3 1.3 0 010 2.6H9.5" />
  </>,
);
export const IconFileText = icon(
  <>
    <path d="M7 3h7l5 5v13H7z" />
    <path d="M14 3v5h5M10 12h6M10 15h6M10 18h4" />
  </>,
);
export const IconUsers = icon(
  <>
    <circle cx="9" cy="8" r="3.2" />
    <path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5" />
    <path d="M16 5.2a3 3 0 010 5.6M18 14.8c2 .6 3.5 2.3 3.5 5.2" />
  </>,
);
export const IconUserPlus = icon(
  <>
    <circle cx="9" cy="8" r="3.2" />
    <path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5M19 8v6M16 11h6" />
  </>,
);
export const IconLogin = icon(<path d="M10 5H6a2 2 0 00-2 2v10a2 2 0 002 2h4M14 8l4 4-4 4M18 12H9" />);
export const IconLogout = icon(<path d="M14 5h4a2 2 0 012 2v10a2 2 0 01-2 2h-4M10 8l-4 4 4 4M6 12h9" />);
export const IconCompass = icon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M15.5 8.5L13.5 13.5 8.5 15.5 10.5 10.5z" />
  </>,
);
export const IconSettings = icon(
  <>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" />
  </>,
);
export const IconPaperclip = icon(<path d="M20 11.5l-8 8a5 5 0 01-7-7l8.5-8.5a3.3 3.3 0 014.7 4.7L9.7 17.2a1.7 1.7 0 01-2.4-2.4L15 7" />);
export const IconSend = icon(<path d="M4 12l16-8-6 16-3-6.5z M11 13.5L20 4" />);
export const IconUpload = icon(<path d="M12 16V5M7.5 9.5L12 5l4.5 4.5M5 19h14" />);
export const IconDownload = icon(<path d="M12 5v11M7.5 11.5L12 16l4.5-4.5M5 19h14" />);
export const IconRefresh = icon(<path d="M19 8a7.5 7.5 0 00-13-1.5L4 9M4 5v4h4M5 16a7.5 7.5 0 0013 1.5L20 15M20 19v-4h-4" />);
export const IconSearch = icon(
  <>
    <circle cx="11" cy="11" r="6" />
    <path d="M16 16l4.5 4.5" />
  </>,
);
export const IconTrash = icon(<path d="M5 7h14M10 7V4.5h4V7M7 7l1 13h8l1-13M10.5 11v6M13.5 11v6" />);
export const IconEdit = icon(<path d="M4 20l1-4L16.5 4.5a2 2 0 013 3L8 19z M14.5 6.5l3 3" />);
export const IconCopy = icon(
  <>
    <rect x="8" y="8" width="11" height="12" rx="2" />
    <path d="M5 16V6a2 2 0 012-2h8" />
  </>,
);
export const IconLink = icon(<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1" />);
export const IconInfo = icon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8v.2" />
  </>,
);
export const IconChat = icon(<path d="M5 5h14a2 2 0 012 2v8a2 2 0 01-2 2h-7l-5 4v-4H5a2 2 0 01-2-2V7a2 2 0 012-2z" />);
export const IconSidebar = icon(
  <>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
  </>,
);
export const IconPanelRight = icon(
  <>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M15 4v16" />
  </>,
);
export const IconSparkle = icon(<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.7 1.8L21.5 18.5l-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7z" />);
export const IconShield = icon(<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z" />);

// ---- the editor toolbar
export const IconUndo = icon(<path d="M9 8H15a5 5 0 010 10H8M9 8L5.5 11.5M9 8L5.5 4.5" transform="translate(0 1)" />);
export const IconRedo = icon(<path d="M15 8H9a5 5 0 000 10h7M15 8l3.5 3.5M15 8l3.5-3.5" transform="translate(0 1)" />);
export const IconBold = icon(<path d="M7 5h6a3.5 3.5 0 010 7H7zM7 12h7a3.5 3.5 0 010 7H7z" />);
export const IconItalic = icon(<path d="M10 5h8M6 19h8M14.5 5l-5 14" />);
export const IconUnderline = icon(<path d="M7 4v7a5 5 0 0010 0V4M5 20h14" />);
export const IconStrike = icon(<path d="M5 12h14M16.5 7.5A4.5 3.5 0 0012 5c-2.5 0-4.5 1.3-4.5 3.2 0 1.8 1.7 2.6 4.5 3.3M8 16.5A4.5 3.5 0 0012 19c2.7 0 4.5-1.2 4.5-3" />);
export const IconSuperscript = icon(<path d="M5 18l7-9M12 18L5 9M16 7l3-3h-3M19 7h-3" />);
export const IconSubscript = icon(<path d="M5 15l7-9M12 15L5 6M16 21l3-3h-3M19 21h-3" />);
export const IconQuote = icon(<path d="M5 17v-4a4 4 0 014-4M12 17v-4a4 4 0 014-4M5 17h4v-4H5zM12 17h4v-4h-4z" />);
export const IconTextColor = icon(<path d="M7 16l5-11 5 11M9 12h6M5 20h14" />);
export const IconHighlight = icon(<path d="M5 20h14M8 15l7-7 3 3-7 7H8zM14 9l3 3" />);
export const IconClearFormat = icon(<path d="M6 5h12M12 5l-3 14M9 19h6M5 20l15-15" />);
export const IconAlignLeft = icon(<path d="M4 6h16M4 10h10M4 14h16M4 18h10" />);
export const IconAlignCenter = icon(<path d="M4 6h16M7 10h10M4 14h16M7 18h10" />);
export const IconAlignRight = icon(<path d="M4 6h16M10 10h10M4 14h16M10 18h10" />);
export const IconAlignJustify = icon(<path d="M4 6h16M4 10h16M4 14h16M4 18h16" />);
export const IconListBullets = icon(
  <>
    <path d="M9 6h11M9 12h11M9 18h11" />
    <circle cx="4.5" cy="6" r="1" fill="currentColor" />
    <circle cx="4.5" cy="12" r="1" fill="currentColor" />
    <circle cx="4.5" cy="18" r="1" fill="currentColor" />
  </>,
);
export const IconListNumbers = icon(<path d="M10 6h10M10 12h10M10 18h10M4 5l1.5-1v4M4 11.5h2.5L4 14.5h2.5M4 17h2.2c.8 0 .8 1.4 0 1.4H5 6.3c.8 0 .8 1.6 0 1.6H4" />);
export const IconIndentIn = icon(<path d="M4 6h16M11 10h9M11 14h9M4 18h16M4 9.5l3 2.5-3 2.5z" />);
export const IconIndentOut = icon(<path d="M4 6h16M11 10h9M11 14h9M4 18h16M7 9.5L4 12l3 2.5z" />);
export const IconImage = icon(
  <>
    <rect x="3.5" y="5" width="17" height="14" rx="2" />
    <circle cx="9" cy="10" r="1.6" />
    <path d="M4 17l5-4.5 4 3 3-2.5 4 3.5" />
  </>,
);
export const IconTable = icon(
  <>
    <rect x="3.5" y="5" width="17" height="14" rx="2" />
    <path d="M3.5 10h17M3.5 14.5h17M9.5 5v14M15 5v14" />
  </>,
);
export const IconDiagram = icon(
  <>
    <rect x="3" y="4" width="7" height="5" rx="1" />
    <rect x="14" y="4" width="7" height="5" rx="1" />
    <rect x="8.5" y="15" width="7" height="5" rx="1" />
    <path d="M6.5 9v2.5h11V9M12 11.5V15" />
  </>,
);
export const IconTypography = icon(<path d="M4 7V5h16v2M12 5v14M9 19h6" />);
