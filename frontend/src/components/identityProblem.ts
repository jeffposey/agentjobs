/**
 * The headline above an identity refusal, chosen from its problem code.
 *
 * Two components used to inline `problem === "multiple" ? … : "No user configured."`,
 * which was exhaustive while there were two problems and stopped being so the moment
 * task-330 added four more: a login that maps to nobody would have been announced as
 * "No user configured", sending the reader to the wrong file. The codes are the
 * backend's `agentjobs.actors` constants, and the fallback is deliberately the
 * vaguest sentence rather than the most likely one -- a wrong specific headline is
 * worse than a general one, because the detail underneath is always right.
 */
export function identityHeadline(problem: string | null | undefined): string {
  switch (problem) {
    case "unmapped":
      return "Your login is not mapped. ";
    case "retired":
      return "That person has retired. ";
    case "unknown_actor":
      return "Mapped to an actor this project does not define. ";
    case "ambiguous":
      return "Cannot tell who is asking. ";
    case "not_a_person":
      return "This is not a person. ";
    case "unconfigured":
      return "No user configured. ";
    default:
      return "Cannot act as anyone. ";
  }
}
