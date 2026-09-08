<!-- include start from serial/service/serial-tunnel-not-profileable.xml.i -->
<node name="serial-tunnel">
  <properties>
    <help>Serial tunnel service settings</help>
  </properties>
  <children>
    <node name="server">
      <properties>
        <help>Serial tunnel server settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/listen-port.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
