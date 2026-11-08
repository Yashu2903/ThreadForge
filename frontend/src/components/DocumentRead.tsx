import type { CSSProperties, ReactNode } from "react";
import type {
  Block,
  DocumentContent,
  Inline,
  List,
  Paragraph,
  ParagraphAttrs,
  Heading,
} from "threadforge-client/document-schema";

/**
 * A read-only rendering of the document tree as React elements. Everything is rendered as text
 * nodes and attributes: no innerHTML, so document content (or a link) can never inject markup.
 * Used for viewers on devices where editing is unsupported and wherever an editor is not mounted.
 * Colours are absolute, so the "paper" always has a light background (see .paper in styles.css).
 */
export function DocumentRead(props: { content: DocumentContent; roomId: string }) {
  return (
    <div className="paper" data-testid="document-read">
      {props.content.blocks.map((block) => (
        <BlockView key={block.id} block={block} roomId={props.roomId} />
      ))}
    </div>
  );
}

function paragraphStyle(attrs: ParagraphAttrs | undefined): CSSProperties {
  if (!attrs) return {};
  return {
    textAlign: attrs.align,
    lineHeight: attrs.line_spacing,
    marginTop: attrs.space_before_pt === undefined ? undefined : `${attrs.space_before_pt}pt`,
    marginBottom: attrs.space_after_pt === undefined ? undefined : `${attrs.space_after_pt}pt`,
    marginLeft: attrs.indent_level ? `${attrs.indent_level * 36}pt` : undefined,
  };
}

function BlockView({ block, roomId }: { block: Block; roomId: string }): ReactNode {
  switch (block.type) {
    case "paragraph":
      return <ParagraphView p={block} roomId={roomId} />;
    case "heading":
      return <HeadingView h={block} roomId={roomId} />;
    case "quote":
      return (
        <blockquote data-id={block.id}>
          {block.children.map((p) => (
            <ParagraphView key={p.id} p={p} roomId={roomId} />
          ))}
        </blockquote>
      );
    case "list":
      return <ListView list={block} roomId={roomId} />;
    case "table":
      return (
        <table data-id={block.id}>
          <tbody>
            {block.children.map((row) => (
              <tr key={row.id} data-id={row.id}>
                {row.children.map((cell) => {
                  const Tag = cell.header ? "th" : "td";
                  return (
                    <Tag key={cell.id} data-id={cell.id}>
                      {cell.children.map((p) => (
                        <ParagraphView key={p.id} p={p} roomId={roomId} />
                      ))}
                    </Tag>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      );
  }
}

function ParagraphView({ p, roomId }: { p: Paragraph; roomId: string }) {
  return (
    <p data-id={p.id} style={paragraphStyle(p.attrs)}>
      <Inlines items={p.children} roomId={roomId} />
    </p>
  );
}

function HeadingView({ h, roomId }: { h: Heading; roomId: string }) {
  const Tag = (["h1", "h2", "h3"] as const)[h.level - 1]!;
  return (
    <Tag data-id={h.id} style={paragraphStyle(h.attrs)}>
      <Inlines items={h.children} roomId={roomId} />
    </Tag>
  );
}

function ListView({ list, roomId }: { list: List; roomId: string }) {
  const Tag = list.ordered ? "ol" : "ul";
  return (
    <Tag data-id={list.id}>
      {list.children.map((item) => (
        <li key={item.id} data-id={item.id}>
          {item.children.map((child) =>
            child.type === "paragraph" ? (
              <ParagraphView key={child.id} p={child} roomId={roomId} />
            ) : (
              <ListView key={child.id} list={child} roomId={roomId} />
            ),
          )}
        </li>
      ))}
    </Tag>
  );
}

function Inlines({ items, roomId }: { items: Inline[] | undefined; roomId: string }) {
  if (!items || items.length === 0) return <br />;
  return (
    <>
      {items.map((node, i) => {
        if (node.type === "hard_break") return <br key={i} />;
        if (node.type === "image") {
          return (
            <img
              key={node.id}
              data-id={node.id}
              src={`/rooms/${roomId}/assets/${node.asset_id}`}
              alt={node.alt ?? ""}
              width={node.width_px}
              height={node.height_px}
            />
          );
        }
        let out: ReactNode = node.text;
        const m = node.marks;
        if (m) {
          if (m.bold) out = <strong>{out}</strong>;
          if (m.italic) out = <em>{out}</em>;
          if (m.underline) out = <u>{out}</u>;
          if (m.strike) out = <s>{out}</s>;
          if (m.script === "superscript") out = <sup>{out}</sup>;
          if (m.script === "subscript") out = <sub>{out}</sub>;
          const style: CSSProperties = {
            fontSize: m.size_pt ? `${m.size_pt}pt` : undefined,
            color: m.color,
            backgroundColor: m.highlight,
          };
          out = (
            <span className={m.font ? `font-${m.font}` : undefined} style={style}>
              {out}
            </span>
          );
          if (m.link) {
            out = (
              <a href={m.link.href} target="_blank" rel="noopener noreferrer">
                {out}
              </a>
            );
          }
        }
        return <span key={i}>{out}</span>;
      })}
    </>
  );
}
