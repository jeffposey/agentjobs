import type { LucideIcon } from "lucide-react";
import {
  BatteryLow,
  Check,
  CheckCheck,
  CirclePlay,
  Clock,
  Cog,
  Copy,
  Eye,
  FilePen,
  Footprints,
  Hand,
  Hourglass,
  Link as LinkIcon,
  Loader,
  MessageCircleQuestionMark,
  MessageSquare,
  Pause,
  PencilRuler,
  PlaneLanding,
  Replace,
  Split,
  TriangleAlert,
  X,
} from "lucide-react";

/**
 * Every icon a status chip can draw, by the Lucide name `status_vocabulary.json` uses
 * (task-578).
 *
 * An explicit list rather than `import *` so the bundle carries these glyphs and no
 * others. Choosing an icon is an edit to the data file plus one line here; a name in the
 * file with no line here fails `statusIcons.test.ts` rather than the page, and a line
 * here that nothing in the file names fails it too, so this stays the used set.
 */
export const STATUS_ICONS: Record<string, LucideIcon> = {
  "battery-low": BatteryLow,
  check: Check,
  "check-check": CheckCheck,
  "circle-play": CirclePlay,
  clock: Clock,
  cog: Cog,
  copy: Copy,
  eye: Eye,
  "file-pen": FilePen,
  footprints: Footprints,
  hand: Hand,
  hourglass: Hourglass,
  link: LinkIcon,
  loader: Loader,
  "message-circle-question-mark": MessageCircleQuestionMark,
  "message-square": MessageSquare,
  pause: Pause,
  "pencil-ruler": PencilRuler,
  "plane-landing": PlaneLanding,
  replace: Replace,
  split: Split,
  "triangle-alert": TriangleAlert,
  x: X,
};
