/**
 * The browser's view of a `diagram` node (the schema itself lives in @threadforge/editor-schema).
 *
 * The Mermaid source is the node's TEXT CONTENT, so there are two things on the page for one node:
 *   * the picture, drawn by SafeMermaid (static rules, strict Mermaid config, timeout, DOMPurify; it re-checks
 *     the text on every draw, so even a source that was typed or merged in by someone else is never given to
 *     Mermaid unless it passes);
 *   * the editable text itself (`NodeViewContent`, ProseMirror's own content DOM). It is always in the page,
 *     because a node view needs its content DOM, but it is visible only while "Edit source" is on and only for
 *     people who can edit. Viewers see the picture alone.
 * Typing in the source is an ordinary text edit, so Yjs merges two people's edits character by character.
 * The picture is redrawn a moment after typing stops, not on every key.
 */
import { NodeViewContent, NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { useEffect, useState } from "react";
import { SafeMermaid } from "../ai/SafeMermaid";

const REDRAW_DELAY_MS = 400;

export function DiagramView({ node, editor }: NodeViewProps) {
  const [editing, setEditing] = useState(false);
  const source = node.textContent;
  const [drawn, setDrawn] = useState(source);
  const canEdit = editor.isEditable;

  useEffect(() => {
    const timer = setTimeout(() => setDrawn(source), REDRAW_DELAY_MS);
    return () => clearTimeout(timer);
  }, [source]);

  const showSource = canEdit && editing;
  return (
    <NodeViewWrapper className="diagram-block" data-testid="diagram-block" data-diagram-block>
      <div contentEditable={false} className="diagram-render">
        <SafeMermaid source={drawn} title="" showSource={false} />
        {canEdit && (
          <button
            type="button"
            className="diagram-toggle"
            aria-pressed={editing}
            data-testid="diagram-edit-toggle"
            onClick={() => setEditing((on) => !on)}
          >
            {editing ? "Hide source" : "Edit source"}
          </button>
        )}
      </div>
      <NodeViewContent
        as="div"
        className="diagram-edit"
        data-testid="diagram-source"
        aria-label="Diagram source"
        style={{ display: showSource ? "block" : "none" }}
      />
    </NodeViewWrapper>
  );
}

/** Passed to createExtensions({ diagramView }) by the browser editor. */
export const diagramNodeView = ReactNodeViewRenderer(DiagramView);
