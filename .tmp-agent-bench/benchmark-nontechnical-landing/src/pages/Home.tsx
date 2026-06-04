import Card from "../components/ui/Card";

const services = [
  {
    title: "Laundry Kilograman",
    desc: "Cuci baju, setrika, dan lipat rapi. Harga termasuk antar-jemput."
  },
  {
    title: "Laundry Satuan",
    desc: "Setrika per item untuk baju formal, kemeja, atau baju khusus."
  },
  {
    title: "Laundry Premium",
    desc: "Dry cleaning untuk baju berbahan halus, sutra, atau wool."
  }
];

const prices = [
  {
    name: "Reguler",
    price: "Rp 7.000/kg",
    features: ["Cuci + Setrika", "Antar-Jemput", "Paket Hemat"],
    recommended: false
  },
  {
    name: "Premium",
    price: "Rp 10.000/kg",
    features: ["Cuci + Setrika + Lipat", "Antar-Jemput", "Deterjen Premium", "Packing Rapi"],
    recommended: true
  },
  {
    name: "Express",
    price: "Rp 12.000/kg",
    features: ["Cuci + Setrika", "Antar-Jemput", "Prioritas Proses", "Packing Rapi"],
    recommended: false
  }
];

const testimonials = [
  {
    name: "Siti Aminah",
    role: "Ibu Rumah Tangga",
    text: "Laundry ini sangat memuaskan. Baju bersih, rapi, dan antar-jemputnya tepat waktu. Harga terjangkau juga."
  },
  {
    name: "Budi Santoso",
    role: "Karyawan Swasta",
    text: "Sering pakai jasa laundry ini karena praktis dan hasilnya bersih. Khususnya layanan express sangat membantu."
  },
  {
    name: "Dewi Lestari",
    role: "Freelancer",
    text: "Pelayanan ramah, baju dikembalikan dalam keadaan rapi dan wangi. Harga bersaing dengan kualitas terbaik."
  }
];

export default function HomePage() {
  return (
    <div className="stack">
      {/* Hero Section */}
      <section className="hero">
        <div className="heroInner">
          <h1 className="heroTitle">Laundry Premium <br/>Bersih, Rapi, Tepat Waktu</h1>
          <p className="heroLead">Jasa laundry terpercaya dengan layanan lengkap. Cuci, setrika, dan antar-jemput tanpa repot.</p>
          <div className="row">
            <a href="#booking" className="btn btnPrimary">Booking Sekarang</a>
            <a href="#pricing" className="btn btnGhost">Lihat Harga</a>
          </div>
        </div>
      </section>

      {/* Services Section */}
      <section className="templatePanel">
        <div className="templatePanelHeader">
          <div>
            <h2>Layanan Kami</h2>
            <p>Pilih paket yang sesuai dengan kebutuhan Anda.</p>
          </div>
        </div>
        <div className="grid">
          {services.map((item) => (
            <Card key={item.title} title={item.title} eyebrow="Layanan">
              <p className="muted">{item.desc}</p>
            </Card>
          ))}
        </div>
      </section>

      {/* Pricing Section */}
      <section id="pricing" className="templatePanel">
        <div className="templatePanelHeader">
          <div>
            <h2>Paket Harga</h2>
            <p>Pilih paket yang paling cocok untuk Anda.</p>
          </div>
        </div>
        <div className="grid">
          {prices.map((plan) => (
            <Card key={plan.name} title={plan.name} className={plan.recommended ? "recommended" : ""}>
              <div className="priceTag">{plan.price}</div>
              <ul className="list">
                {plan.features.map((feat) => (
                  <li key={feat}>{feat}</li>
                ))}
              </ul>
              <div className="row" style={{ marginTop: "16px" }}>
                <a href="#booking" className="btn btnPrimary" style={{ flex: 1, textAlign: "center" }}>Pilih Paket</a>
              </div>
            </Card>
          ))}
        </div>
      </section>

      {/* Testimonials Section */}
      <section className="templatePanel">
        <div className="templatePanelHeader">
          <div>
            <h2>Testimoni Pelanggan</h2>
            <p>Apa kata mereka tentang layanan kami.</p>
          </div>
        </div>
        <div className="grid">
          {testimonials.map((item) => (
            <Card key={item.name} title={item.name} className="testimonial">
              <p className="muted">"{item.text}"</p>
              <div className="eyebrow" style={{ marginTop: "12px" }}>{item.role}</div>
            </Card>
          ))}
        </div>
      </section>

      {/* Booking CTA Section */}
      <section id="booking" className="hero">
        <div className="heroInner">
          <h2 className="heroTitle">Siap Memulai?</h2>
          <p className="heroLead">Booking sekarang dan dapatkan diskon 10% untuk pesanan pertama Anda.</p>
          <div className="row">
            <a href="mailto:laundry@premium.com?subject=Booking%20Laundry%20Premium" className="btn btnPrimary">Booking Sekarang</a>
            <a href="tel:kontak-belum-dikonfigurasi" className="btn btnGhost">Hubungi WhatsApp</a>
          </div>
        </div>
      </section>
    </div>
  );
}
