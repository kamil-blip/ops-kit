-- vec-side schema: apply with sqlite-vec loaded (init_db.py does this).

CREATE VIRTUAL TABLE vec_emails USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_entities USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_episodes USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_faqs USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_learnings USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_observations USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_people USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_reference_doc_chunks USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE vec_action_items USING vec0(embedding float[384]);
