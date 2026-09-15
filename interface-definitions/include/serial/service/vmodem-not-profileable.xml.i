<!-- include start from serial/service/vmodem-not-profileable.xml.i -->
<node name="virtual-modem">
  <properties>
    <help>Virtual Modem service settings</help>
  </properties>
  <children>
    <node name="server">
       <properties>
          <help>Virtual Modem server settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/listen-port.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
